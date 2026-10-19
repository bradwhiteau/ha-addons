"""Email Triage add-on.

Home Assistant's IMAP integration fires an `imap_content` event as soon as new
mail lands. That event is only used as a wake-up signal: the add-on then reads
every inbox message it hasn't handled yet (the event only describes the newest
one), so nothing is missed when several emails arrive together. A sweep every
few minutes catches anything if an event is ever dropped.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import websocket

import actions
from llm import Claude
from mail import Email, Mailbox, one_click_unsubscribe_url

OPTIONS_FILE = "/data/options.json"
STATE_FILE = "/data/state.json"
WS_URL = "ws://supervisor/core/websocket"
TZ = ZoneInfo("Australia/Perth")
MAX_ATTEMPTS = 3

LABEL_DONE = "AI/Triaged"
LABEL_MARKETING = "AI/Marketing"
LABEL_UNSUB = "AI/Unsubscribed"
LABEL_BILL = "AI/Bill"
LABEL_URGENT = "AI/Urgent"
LABEL_DRAFT_READY = "AI/Draft ready"
LABEL_DRAFT = "AI draft"

log = logging.getLogger("triage")
wake = queue.Queue()


# ---------------------------------------------------------------- config & state

def load_config() -> dict:
    with open(OPTIONS_FILE) as f:
        cfg = json.load(f)
    missing = [k for k in ("gmail_address", "gmail_app_password", "anthropic_api_key") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"Missing settings: {', '.join(missing)}. Fill them in on the add-on's Configuration tab.")
    cfg["gmail_app_password"] = cfg["gmail_app_password"].replace(" ", "")
    return cfg


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- HA event listener

def listen_for_mail_events() -> None:
    """Subscribe to imap_content events; any event wakes the worker."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log.warning("No SUPERVISOR_TOKEN: relying on the timed sweep only")
        return
    while True:
        try:
            ws = websocket.create_connection(WS_URL, timeout=30)
            ws.recv()  # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"HA auth failed: {auth}")
            ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "imap_content"}))
            ws.settimeout(None)
            log.info("Listening for new-mail events from Home Assistant")
            while True:
                msg = json.loads(ws.recv())
                if msg.get("type") == "event":
                    data = msg["event"].get("data", {})
                    log.debug("imap_content: %s | %s", data.get("sender"), data.get("subject"))
                    wake.put("event")
        except Exception as exc:
            log.warning("Event connection lost (%s); reconnecting in 30s", exc)
            time.sleep(30)


# ---------------------------------------------------------------- triage logic

class Triage:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dry = cfg["dry_run"]
        self.box = Mailbox(cfg["gmail_address"], cfg["gmail_app_password"], self.dry)
        self.claude = Claude(cfg["anthropic_api_key"], cfg["triage_model"], cfg["reasoning_model"])
        self.state = load_state()

    def tag(self, text: str) -> str:
        return f"[dry run] {text}" if self.dry else text

    def init_position(self) -> None:
        uidvalidity, uidnext = self.box.inbox_status()
        if self.state.get("uidvalidity") != uidvalidity:
            # first run (or Gmail reset UIDs): start from now, don't backfill old mail
            self.state = {"uidvalidity": uidvalidity, "last_uid": uidnext - 1, "attempts": {}}
            save_state(self.state)
            log.info("Starting fresh: only mail arriving from now on will be triaged")

    def run_once(self) -> None:
        self.init_position()
        uids = self.box.uids_after(self.state["last_uid"])
        for uid in uids:
            done = self.handle_uid(uid)
            if not done:
                break  # retry this one (and later ones) on the next wake-up
            self.state["last_uid"] = uid
            self.state.get("attempts", {}).pop(str(uid), None)
            save_state(self.state)

    def handle_uid(self, uid: int) -> bool:
        attempts = self.state.setdefault("attempts", {})
        try:
            em = self.box.fetch(uid)
            if em is None:
                return True  # moved or deleted before we got to it
            self.process(em)
            return True
        except Exception as exc:
            n = attempts.get(str(uid), 0) + 1
            attempts[str(uid)] = n
            save_state(self.state)
            log.exception("Failed on UID %s (attempt %s/%s): %s", uid, n, MAX_ATTEMPTS, exc)
            self.box.close()
            if n >= MAX_ATTEMPTS:
                log.error("Giving up on UID %s after %s attempts", uid, n)
                return True
            return False

    def process(self, em: Email) -> None:
        if LABEL_DONE in em.gmail_labels:
            return
        if em.from_addr == self.cfg["gmail_address"].lower():
            return  # Brad's own mail

        t = self.claude.triage(em)
        log.info("%s | %s | kind=%s bill=%s urgent=%s reply=%s",
                 em.from_addr, em.subject[:60], t["kind"], t["is_bill"], t["is_urgent"], t["reply_warranted"])

        labels = [LABEL_DONE]
        done: list[str] = []
        now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

        # 1. junk
        if t["kind"] == "spam":
            self.box.add_labels(em.uid, labels)
            self.box.move_to_spam(em.uid)
            done.append("moved to Spam")
            actions.sheet_log(self.cfg, "Unsubscribes", [
                now, em.from_name, em.from_addr, em.subject, self.tag("Marked as spam (not unsubscribed)"), em.link, t["summary"],
            ])
            self.log_row(now, em, t, done)
            return

        if t["kind"] == "marketing":
            labels.append(LABEL_MARKETING)
            url, why = one_click_unsubscribe_url(em)
            if url and self.cfg["auto_unsubscribe"]:
                ok, info = actions.one_click_unsubscribe(url, self.dry)
                if ok:
                    labels.append(LABEL_UNSUB)
                    action = "Unsubscribed (one-click)"
                else:
                    action = f"Unsubscribe failed ({info}) - check manually"
            elif url:
                action = "Not unsubscribed (auto-unsubscribe is off)"
            else:
                action = f"Not unsubscribed: {why} - check manually"
            done.append(action)
            actions.sheet_log(self.cfg, "Unsubscribes", [
                now, em.from_name, em.from_addr, em.subject, self.tag(action), em.link, t["summary"],
            ])

        # 2. urgent alert
        if t["is_urgent"] and t["kind"] != "marketing":
            labels.append(LABEL_URGENT)
            actions.notify(
                self.cfg["notify_service"],
                f"📧 Urgent: {em.from_name or em.from_addr}",
                f"{em.subject}\n{t.get('urgent_reason') or t['summary']}",
                em.link,
                self.dry,
            )
            done.append("phone alert")

        # 3. bills
        if t["is_bill"] and t["kind"] == "normal":
            labels.append(LABEL_BILL)
            bill = self.claude.extract_bill(em)
            if bill.get("already_paid_or_autopay"):
                done.append("bill (already paid / direct debit, no task)")
            elif not self.cfg.get("todoist_api_token"):
                done.append("bill (no Todoist token set)")
            else:
                changed = actions.check_bank_details(em, bill, self.dry)
                title, desc = actions.bill_task_text(em, bill, changed)
                task = actions.add_todoist_task(self.cfg, title, desc, bill.get("due_date"), bool(changed) or t["is_urgent"], self.dry)
                done.append(f"Todoist: {title}" if task else "Todoist task FAILED")
                if changed:
                    actions.notify(
                        self.cfg["notify_service"],
                        "⚠️ Bill bank details changed",
                        f"{bill.get('biller')}: {', '.join(changed)} differ from last time. Verify before paying.",
                        em.link,
                        self.dry,
                    )

        # 4. draft reply
        if t["reply_warranted"] and t["kind"] == "normal" and self.cfg["create_drafts"]:
            d = self.claude.draft_reply(em)
            if d.get("should_draft") and d.get("body", "").strip():
                raw = actions.build_reply(em, self.cfg["gmail_address"], d["body"])
                if self.box.save_draft(raw, LABEL_DRAFT):
                    labels.append(LABEL_DRAFT_READY)
                    done.append(f"draft saved: {d.get('note_for_brad', '')}")

        self.box.add_labels(em.uid, labels)
        self.log_row(now, em, t, done)

    def log_row(self, now: str, em: Email, t: dict, done: list[str]) -> None:
        flags = ", ".join(f for f, on in (("bill", t["is_bill"]), ("urgent", t["is_urgent"]), ("reply", t["reply_warranted"])) if on)
        actions.sheet_log(self.cfg, "Triage log", [
            now, em.from_name, em.from_addr, em.subject, t["kind"], flags, t["summary"],
            self.tag("; ".join(done) or "labelled only"), em.link,
        ])


# ---------------------------------------------------------------- main loop

def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("Email Triage starting%s", " in DRY RUN mode (no changes will be made)" if cfg["dry_run"] else "")

    triage = Triage(cfg)
    threading.Thread(target=listen_for_mail_events, daemon=True).start()

    sweep = cfg.get("sweep_minutes", 10) * 60
    wake.put("startup")
    while True:
        try:
            wake.get(timeout=sweep)
        except queue.Empty:
            pass
        time.sleep(3)  # let a burst of arrivals settle
        while not wake.empty():
            wake.get_nowait()
        try:
            triage.run_once()
        except Exception as exc:
            log.exception("Run failed: %s", exc)
            triage.box.close()
            time.sleep(30)


if __name__ == "__main__":
    main()
