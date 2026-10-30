"""Email Triage add-on.

Home Assistant's IMAP integration fires an `imap_content` event as soon as new
mail lands. That event is only used as a wake-up signal: the add-on then reads
every inbox message it hasn't handled yet (the event only describes the newest
one), so nothing is missed when several emails arrive together. A sweep every
few minutes catches anything if an event is ever dropped.
"""
from __future__ import annotations

import imaplib
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import websocket

import actions
from career import Career
from deliveries import Deliveries
from llm import Claude
from subscriptions import Subscriptions
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
LABEL_JOBS = "AI/Job alert"
LABEL_TIMESHEET = "AI/Timesheet confirmed"
LABEL_SUBSCRIPTION = "AI/Subscription"
LABEL_NEEDS_YOU = "AI/Attention"            # triaged and deliberately left in the inbox for Brad
LABEL_UNSUB_MANUAL = "AI/Unsubscribe manually"
LABEL_KEEP = "AI/Keep list"                 # sender is on never_unsubscribe

log = logging.getLogger("triage")
wake = queue.Queue()
career_actions = queue.Queue()
catchup_requests = queue.Queue()


# ---------------------------------------------------------------- config & state

def load_config() -> dict:
    with open(OPTIONS_FILE) as f:
        cfg = json.load(f)
    missing = [k for k in ("gmail_address", "gmail_app_password", "anthropic_api_key") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"Missing settings: {', '.join(missing)}. Fill them in on the add-on's Configuration tab.")
    # app passwords are shown as "abcd efgh ijkl mnop"; drop any spaces or stray newlines
    cfg["gmail_app_password"] = "".join(cfg["gmail_app_password"].split())
    cfg["gmail_address"] = cfg["gmail_address"].strip()
    for key in ("anthropic_api_key", "todoist_api_token", "sheet_webhook_secret", "sheet_webhook_url"):
        if isinstance(cfg.get(key), str):
            cfg[key] = cfg[key].strip()
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

def supervisor_token() -> str | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        try:
            with open("/run/s6/container_environment/SUPERVISOR_TOKEN") as f:
                token = f.read().strip()
            os.environ["SUPERVISOR_TOKEN"] = token
        except OSError:
            token = None
    return token or None


def listen_for_mail_events() -> None:
    """Subscribe to imap_content events; any event wakes the worker."""
    token = supervisor_token()
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
            ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "mobile_app_notification_action"}))
            ws.send(json.dumps({"id": 3, "type": "subscribe_events", "event_type": "email_triage_command"}))
            ws.settimeout(None)
            log.info("Listening for new-mail events from Home Assistant")
            while True:
                msg = json.loads(ws.recv())
                if msg.get("type") != "event":
                    continue
                event = msg["event"]
                data = event.get("data", {})
                if event.get("event_type") == "mobile_app_notification_action":
                    action = str(data.get("action", ""))
                    if action.startswith("CAREER_"):
                        log.info("Button pressed: %s", action)
                        career_actions.put(action)
                    continue
                if event.get("event_type") == "email_triage_command":
                    cmd = str(data.get("command", ""))
                    log.info("Command received: %s", cmd)
                    if cmd == "reask_jobs":
                        career_actions.put("REASK")
                    elif cmd == "retry_failed":
                        career_actions.put("RETRY")
                    elif cmd == "catch_up":
                        catchup_requests.put(str(data.get("since", "")))
                        wake.put("catch_up")
                    elif cmd == "rerun_jobs":
                        catchup_requests.put("jobs:" + str(data.get("since", "")))
                        wake.put("catch_up")
                    continue
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
        self.career = Career(cfg, self.claude) if cfg.get("career_enabled", True) else None
        self.deliveries = (Deliveries(cfg, self.claude)
                           if cfg.get("deliveries_enabled", True) and cfg.get("todoist_deliveries_project_id")
                           and cfg.get("todoist_api_token") else None)
        self.subscriptions = (Subscriptions(cfg, self.claude)
                              if cfg.get("subscriptions_enabled", True) and cfg.get("todoist_subscriptions_project_id")
                              and cfg.get("todoist_api_token") else None)
        self.assistant_live = cfg.get("assistant_live", True)
        self.known_apps = [a.strip().lower() for a in cfg.get("known_google_apps", []) if a and a.strip()]
        self.archive_days = int(cfg.get("archive_after_unsubscribe_days", 14) or 0)
        self.catchup = False  # True while re-running mail that arrived during dry run
        self.state = load_state()

    def notify(self, *args, **kwargs) -> None:
        """Phone alert, except during catch-up: those emails are old news."""
        if self.catchup:
            args = list(args)
            if len(args) >= 5:
                args[4] = True
            else:
                kwargs["dry_run"] = True
        actions.notify(*args, **kwargs)

    def run_catchup(self, since: str) -> None:
        """Re-run inbox mail that arrived while dry run was on, as it would have gone live.
        With a "jobs:" prefix, re-run only SEEK job alerts, for real (e.g. after a failed read)."""
        jobs_only = since.startswith("jobs:")
        since = since.removeprefix("jobs:")
        if self.dry:
            log.warning("Catch-up skipped: dry run is still on")
            return
        try:
            day = datetime.strptime(since, "%Y-%m-%d").strftime("%d-%b-%Y")
        except ValueError:
            log.warning("Catch-up needs since=YYYY-MM-DD, got %r", since)
            return
        self.init_position()
        uids = [u for u in self.box.uids_since(day) if u <= self.state["last_uid"]][:400]
        log.info("Catch-up: %s inbox email(s) since %s to check", len(uids), since)
        self.catchup = not jobs_only
        done = 0
        try:
            for uid in uids:
                try:
                    em = self.box.fetch(uid)
                    if em is not None and LABEL_DONE not in em.gmail_labels and (not jobs_only or self.is_job_alert(em)):
                        self.process(em)
                        done += 1
                except Exception as exc:
                    log.exception("Catch-up failed on UID %s: %s", uid, exc)
                    self.box.close()
        finally:
            self.catchup = False
        log.info("Catch-up finished: %s email(s) processed", done)

    # ---------------------------------------------------------- small rules
    def own_google_app(self, em: Email) -> str | None:
        """Google 'app granted access' alerts for Brad's own Apps Script projects."""
        if not (em.from_domain == "google.com" or em.from_domain.endswith(".google.com")):
            return None
        if "security alert" not in em.subject.lower() and "access to your google account" not in em.body.lower():
            return None
        body = em.body.lower()
        return next((a for a in self.known_apps if a in body), None)

    @staticmethod
    def is_job_alert(em: Email) -> bool:
        return em.from_domain.endswith("seek.com.au") and em.from_addr.startswith("jobmail@") and "job" in em.subject.lower()

    @staticmethod
    def is_application_update(em: Email) -> bool:
        """SEEK telling Brad how an application went (not the 'submitted' confirmation)."""
        if not em.from_domain.endswith("seek.com.au") or em.from_addr.startswith("jobmail@"):
            return False
        subj = em.subject.lower()
        if "application" not in subj or "application submitted" in subj:
            return False
        return "application update" in subj or bool(
            re.search(r"unsuccessful|not progress|unlikely to progress|interview|offer", subj + " " + em.body[:1500].lower()))

    def recently_unsubscribed(self, addr: str) -> str | None:
        """Date Brad was unsubscribed from this sender, if within the archive window."""
        when = self.state.get("unsubscribed", {}).get(addr)
        if not when or self.archive_days <= 0:
            return None
        try:
            age = datetime.now(TZ) - datetime.fromisoformat(when)
        except ValueError:
            return None
        return when[:10] if age.days < self.archive_days else None

    def remember_unsubscribe(self, addr: str) -> None:
        if self.dry:
            return
        unsub = self.state.setdefault("unsubscribed", {})
        unsub[addr] = datetime.now(TZ).isoformat()
        cutoff = datetime.now(TZ).timestamp() - 90 * 86400
        for a, w in list(unsub.items()):
            try:
                if datetime.fromisoformat(w).timestamp() < cutoff:
                    del unsub[a]
            except ValueError:
                del unsub[a]
        save_state(self.state)

    def archive_email(self, em: Email) -> None:
        """Archive, and remember it so a later sweep can confirm it really left the inbox
        (Gmail sometimes accepts the change for brand-new mail without applying it)."""
        self.box.archive(em.uid)
        self._archived = True
        if self.dry:
            return
        pending = self.state.setdefault("archive_check", {})
        pending[em.message_id] = {"at": time.time(), "tries": 0, "subject": em.subject[:80]}
        save_state(self.state)

    def verify_archives(self) -> None:
        pending = self.state.get("archive_check") or {}
        if not pending:
            return
        now = time.time()
        for mid, info in list(pending.items()):
            if now - info["at"] < 120:
                continue  # give Gmail a couple of minutes first
            if now - info["at"] > 3600 or info["tries"] >= 3:
                pending.pop(mid, None)  # stop after an hour, in case Brad moved it back on purpose
                if info["tries"] >= 3:
                    log.warning("Could not archive '%s' after 3 tries", info.get("subject"))
                continue
            try:
                uid = self.box.inbox_uid(mid)
            except Exception as exc:
                log.debug("Archive check failed for %s: %s", mid, exc)
                continue
            if uid is None:
                pending.pop(mid, None)  # gone from the inbox: archived as intended
                continue
            info["tries"] += 1
            info["at"] = now - 60  # check again on the next sweep
            log.info("Still in inbox, archiving again: %s", info.get("subject"))
            self.box.archive_by_move(uid)
        save_state(self.state)

    def archive_handled(self, uid: int, done: list | None = None, why: str = "handled", em: Email | None = None) -> None:
        """Inbox zero: once the agent has fully dealt with an email, archive it (never deleted)."""
        if not self.cfg.get("archive_handled", True):
            return
        if em is not None:
            self.archive_email(em)
        else:
            self.box.archive(uid)
            self._archived = True
        if done is not None:
            done.append(f"archived ({why})")

    def tag(self, text: str) -> str:
        if self.catchup:
            return f"[catch-up] {text}"
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
        self.verify_archives()
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
        em = None
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
                if em is not None:
                    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
                    actions.sheet_log(self.cfg, "Triage log", [
                        now, em.from_name, em.from_addr, em.subject, "error", "",
                        "Could not be triaged", f"Skipped after {n} attempts: {str(exc)[:150]}", em.link,
                    ])
                return True
            return False

    def process(self, em: Email) -> None:
        """Triage one email; anything handled but deliberately left in the inbox also gets
        AI/Attention, so Brad can see at a glance it was looked at and is waiting on him."""
        self._archived = False
        self._touched = False
        self._kept = False
        self._process(em)
        if self._touched and not self._archived and not self._kept and self.cfg.get("label_needs_you", True):
            self.box.add_labels(em.uid, [LABEL_NEEDS_YOU])

    def _process(self, em: Email) -> None:
        if LABEL_DONE in em.gmail_labels:
            return
        if em.from_addr == self.cfg["gmail_address"].lower():
            return  # Brad's own mail
        self._touched = True
        now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

        # Already handled for real during dry run (assistant_live): just label them
        if self.catchup and em.from_domain.endswith("oncoreservices.com") and "timesheet received" in em.subject.lower():
            self.box.add_labels(em.uid, [LABEL_DONE, LABEL_TIMESHEET])
            self.archive_handled(em.uid, em=em)
            return
        if self.catchup and self.career and self.is_job_alert(em):
            self.box.add_labels(em.uid, [LABEL_DONE, LABEL_JOBS])
            self.archive_handled(em.uid, em=em)
            return

        # Oncore confirming it received the weekly timesheet
        if em.from_domain.endswith("oncoreservices.com") and "timesheet received" in em.subject.lower():
            m = re.search(r"period ending\s+(\d{1,2}\s+\w+\s+\d{4})", em.body, re.I)
            period = f" for the week ending {m.group(1)}" if m else ""
            self.notify(self.cfg["notify_service"], "✅ Timesheet received",
                           f"Oncore confirmed it received your timesheet{period}.", em.link,
                           not self.assistant_live, channel="Timesheets", style="timesheet")
            self.box.add_labels(em.uid, [LABEL_DONE, LABEL_TIMESHEET])
            tick = "phone tick" if self.assistant_live else "[not live] phone tick"
            if self.assistant_live and self.cfg.get("archive_handled", True):
                self.archive_handled(em.uid, em=em)
                tick += "; archived"
            actions.sheet_log(self.cfg, "Triage log", [
                now, em.from_name, em.from_addr, em.subject, "timesheet", "", f"Oncore timesheet confirmation{period}",
                tick, em.link,
            ])
            return

        # SEEK job alerts go to the Career Counsellor, never to unsubscribe
        if self.career and self.is_job_alert(em):
            try:
                result = self.career.handle_alert(em)
            except Exception as exc:
                log.exception("Career Counsellor failed: %s", exc)
                result = f"Career Counsellor failed: {str(exc)[:150]}"
            self.box.add_labels(em.uid, [LABEL_DONE, LABEL_JOBS])
            if (self.assistant_live and self.cfg.get("archive_handled", True)
                    and not str(result).startswith(("Career Counsellor failed", "[not live]"))):
                self.archive_handled(em.uid, em=em)  # every role is on the Job matches tab
                result = f"{result}; archived"
            actions.sheet_log(self.cfg, "Triage log", [
                now, em.from_name, em.from_addr, em.subject, "job alert", "", "Sent to Career Counsellor", result, em.link,
            ])
            return

        t = self.claude.triage(em)
        t.setdefault("delivery_stage", "none")
        t.setdefault("subscription_event", "none")
        t.setdefault("fyi_only", False)
        log.info("%s | %s | kind=%s bill=%s urgent=%s reply=%s delivery=%s subscription=%s",
                 em.from_addr, em.subject[:60], t["kind"], t["is_bill"], t["is_urgent"], t["reply_warranted"],
                 t["delivery_stage"], t["subscription_event"])

        labels = [LABEL_DONE]
        done: list[str] = []

        # job application outcomes always reach Brad's phone and stay in the inbox
        app_update = self.is_application_update(em)
        if app_update:
            t["is_urgent"] = True
            t["fyi_only"] = False
            t["kind"] = "normal"

        # Brad's own Apps Script projects trigger Google "granted access" alerts: not urgent
        own_app = self.own_google_app(em)
        if own_app and t["is_urgent"]:
            t["is_urgent"] = False
            done.append(f"not alerted: '{own_app}' is on your known apps list")

        # 1. junk
        if t["kind"] == "spam":
            self.box.add_labels(em.uid, labels)
            self.box.move_to_spam(em.uid)
            self._archived = True  # out of the inbox
            done.append("moved to Spam")
            actions.sheet_log(self.cfg, "Unsubscribes", [
                now, em.from_name, em.from_addr, em.subject, self.tag("Marked as spam (not unsubscribed)"), em.link, t["summary"],
            ])
            self.log_row(now, em, t, done)
            return

        if t["kind"] == "marketing":
            labels.append(LABEL_MARKETING)
            url, why = one_click_unsubscribe_url(em)
            protected = any(em.from_domain == d or em.from_domain.endswith("." + d)
                            for d in (x.lower().strip() for x in self.cfg.get("never_unsubscribe", [])) if d)
            archive = False
            already = None if protected else self.recently_unsubscribed(em.from_addr)
            if protected:
                labels.append(LABEL_KEEP)
                self._kept = True  # his own choice to keep it: AI/Keep list says enough
                action = "Not unsubscribed: sender is on your keep list"
            elif already:
                # unsubscribes can take a few days to stick; tidy away stragglers meanwhile
                labels.append(LABEL_UNSUB)
                archive = True
                action = f"Archived: already unsubscribed on {already}"
            elif url and self.cfg["auto_unsubscribe"]:
                ok, info = actions.one_click_unsubscribe(url, self.dry)
                if ok:
                    labels.append(LABEL_UNSUB)
                    self.remember_unsubscribe(em.from_addr)
                    archive = self.archive_days > 0
                    action = "Unsubscribed (one-click)" + (" and archived" if archive else "")
                else:
                    action = f"Unsubscribe failed ({info}) - check manually"
            elif url:
                action = "Not unsubscribed (auto-unsubscribe is off)"
            else:
                action = f"Not unsubscribed: {why} - check manually"
            if "check manually" in action:
                labels.append(LABEL_UNSUB_MANUAL)
            done.append(action)
            actions.sheet_log(self.cfg, "Unsubscribes", [
                now, em.from_name, em.from_addr, em.subject, self.tag(action), em.link, t["summary"],
            ])
            if archive:
                self.box.add_labels(em.uid, labels)
                self.archive_email(em)
                self.log_row(now, em, t, done)
                return

        # 2. urgent alert
        if t["is_urgent"] and t["kind"] != "marketing":
            labels.append(LABEL_URGENT)
            self.notify(
                self.cfg["notify_service"],
                "💼 Application update" if app_update else f"📧 Urgent: {em.from_name or em.from_addr}",
                f"{em.subject}\n{t.get('urgent_reason') or t['summary']}",
                em.link,
                self.dry,
                style="job" if app_update else "urgent",
            )
            done.append("urgent (no phone alert: old email)" if self.catchup else "phone alert")

        # 3. bills
        bill_handled = False
        if t["is_bill"] and t["kind"] == "normal":
            labels.append(LABEL_BILL)
            bill = self.claude.extract_bill(em)
            if bill.get("already_paid_or_autopay"):
                done.append("bill (already paid / direct debit, no task)")
                bill_handled = True
            elif not self.cfg.get("todoist_api_token"):
                done.append("bill (no Todoist token set)")
            else:
                changed = actions.check_bank_details(em, bill, self.dry)
                title, desc = actions.bill_task_text(em, bill, changed)
                task = actions.add_todoist_task(self.cfg, title, desc, bill.get("due_date"), bool(changed) or t["is_urgent"], self.dry)
                done.append(f"Todoist: {title}" if task else "Todoist task FAILED")
                bill_handled = bool(task) and not changed  # a bank-detail change needs Brad's eyes
                if changed:
                    self.notify(
                        self.cfg["notify_service"],
                        "⚠️ Bill bank details changed",
                        f"{bill.get('biller')}: {', '.join(changed)} differ from last time. Verify before paying.",
                        em.link,
                        self.dry,
                        style="bank",
                    )

        # 4. deliveries board
        archive_after = False
        if self.deliveries and t["kind"] == "normal" and t["delivery_stage"] != "none":
            ok = True
            if not self.catchup:  # catch-up: the card was already updated live during dry run
                try:
                    result = self.deliveries.handle(em, t["delivery_stage"])
                except Exception as exc:
                    log.exception("Deliveries update failed: %s", exc)
                    result = f"Deliveries update FAILED ({str(exc)[:80]})"
                    ok = False
                if result:
                    done.append(result)
                ok = ok and bool(result) and not result.startswith("[not live]")
            # the card holds the details and a link back, so tidy the email away;
            # problems stay in the inbox, and so does anything urgent or needing a reply
            if (ok and self.cfg.get("archive_deliveries", True) and t["delivery_stage"] != "problem"
                    and not t["is_urgent"] and not t["reply_warranted"]):
                archive_after = True

        # 5. subscriptions list
        if self.subscriptions and t["kind"] == "normal" and t["subscription_event"] != "none":
            labels.append(LABEL_SUBSCRIPTION)
        sub_handled = False
        if self.subscriptions and t["kind"] == "normal" and t["subscription_event"] != "none":
            sub_handled = True
        if self.subscriptions and t["kind"] == "normal" and t["subscription_event"] != "none" and not self.catchup:
            try:
                result = self.subscriptions.handle(em, t["subscription_event"], is_bill=t["is_bill"])
            except Exception as exc:
                log.exception("Subscriptions update failed: %s", exc)
                result = f"Subscriptions update FAILED ({str(exc)[:80]})"
            if result:
                done.append(result)
            sub_handled = (bool(result) and not result.startswith(("Subscriptions update FAILED", "[not live]"))
                           and not getattr(self.subscriptions, "flagged", False))

        # 6. draft reply
        if t["reply_warranted"] and t["kind"] == "normal" and self.cfg["create_drafts"]:
            d = self.claude.draft_reply(em)
            if d.get("should_draft") and d.get("body", "").strip():
                raw = actions.build_reply(em, self.cfg["gmail_address"], d["body"])
                if self.box.save_draft(raw, LABEL_DRAFT):
                    labels.append(LABEL_DRAFT_READY)
                    done.append(f"draft saved: {d.get('note_for_brad', '')}")

        self.box.add_labels(em.uid, labels)
        needs_brad = t["is_urgent"] or t["reply_warranted"] or (t["is_bill"] and not bill_handled)
        if archive_after and self.cfg.get("archive_handled", True):
            self.archive_email(em)
            done.append("archived (on the Deliveries board)")
        elif not needs_brad and (bill_handled or sub_handled):
            self.archive_handled(em.uid, done, em=em, why="on your Todoist list" if bill_handled else "on the Subscriptions list")
        elif (t["kind"] == "normal" and t["fyi_only"] and not needs_brad and not done
              and t["delivery_stage"] == "none" and t["subscription_event"] == "none"):
            # purely informational: archive (or, while on trial, just note that it would have)
            mode = self.cfg.get("archive_fyi", "trial")
            if mode == "on":
                self.archive_handled(em.uid, done, em=em, why="FYI only")
            elif mode == "trial":
                done.append("[trial] would archive: FYI only")
        self.log_row(now, em, t, done)

    def log_row(self, now: str, em: Email, t: dict, done: list[str]) -> None:
        flags = ", ".join(f for f, on in (("bill", t["is_bill"]), ("urgent", t["is_urgent"]), ("reply", t["reply_warranted"])) if on)
        actions.sheet_log(self.cfg, "Triage log", [
            now, em.from_name, em.from_addr, em.subject, t["kind"], flags, t["summary"],
            self.tag("; ".join(done) or "labelled only"), em.link,
        ])


# ---------------------------------------------------------------- main loop

def career_worker(career: Career) -> None:
    """Handles Interested / Not for me button presses."""
    while True:
        action = career_actions.get()
        try:
            if action == "REASK":
                career.reask_pending()
                continue
            if action == "RETRY":
                career.retry_failed()
                continue
            career.handle_action(action)
        except Exception as exc:
            log.exception("Career action %s failed: %s", action, exc)


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("Email Triage starting%s", " in DRY RUN mode (no changes will be made)" if cfg["dry_run"] else "")

    supervisor_token()  # load it early so notifications can use it too
    triage = Triage(cfg)
    threading.Thread(target=listen_for_mail_events, daemon=True).start()
    if triage.career:
        threading.Thread(target=career_worker, args=(triage.career,), daemon=True).start()

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
            while not catchup_requests.empty():
                triage.run_catchup(catchup_requests.get_nowait())
        except imaplib.IMAP4.error as exc:
            if "AUTHENTICATIONFAILED" in str(exc):
                log.error("Gmail rejected the login. Check gmail_address and gmail_app_password "
                          "(a 16-letter app password, not your normal Google password). Retrying in 5 minutes.")
                triage.box.close()
                time.sleep(300)
            else:
                log.exception("Run failed: %s", exc)
                triage.box.close()
                time.sleep(30)
        except Exception as exc:
            log.exception("Run failed: %s", exc)
            triage.box.close()
            time.sleep(30)


if __name__ == "__main__":
    main()
