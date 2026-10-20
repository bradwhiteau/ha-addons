"""Side effects outside the mailbox: phone alerts via Home Assistant, Todoist
tasks, the Google Sheet log, one-click unsubscribes and draft building."""
from __future__ import annotations

import email.utils
import json
import logging
import os
import re
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

from mail import Email

log = logging.getLogger("actions")
TZ = ZoneInfo("Australia/Perth")
SUPERVISOR = "http://supervisor/core/api"
TODOIST_TASKS = "https://api.todoist.com/api/v1/tasks"
BILLERS_FILE = "/data/billers.json"


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- Home Assistant

def notify(service: str, title: str, message: str, url: str | None, dry_run: bool,
           extra: dict | None = None, channel: str = "Email triage") -> None:
    if dry_run:
        log.info("[dry run] would notify %s: %s | %s", service, title, message)
        return
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log.warning("No SUPERVISOR_TOKEN; cannot send notification")
        return
    data = {"title": title[:80], "message": message[:400]}
    payload = {"channel": channel, "importance": "high"}
    if url:
        payload.update({"clickAction": url, "url": url})
    if extra:
        payload.update(extra)
    data["data"] = payload
    r = requests.post(
        f"{SUPERVISOR}/services/notify/{service}",
        headers={"Authorization": f"Bearer {token}"},
        json=data,
        timeout=20,
    )
    if r.status_code >= 300:
        log.warning("Notification failed (%s): %s", r.status_code, r.text[:200])


# ---------------------------------------------------------------- Google Sheet log

def sheet_log(cfg: dict, sheet: str, row: list) -> None:
    url = cfg.get("sheet_webhook_url")
    if not url:
        log.info("Sheet log (%s, no webhook set): %s", sheet, row)
        return
    try:
        r = requests.post(
            url,
            json={"secret": cfg.get("sheet_webhook_secret", ""), "sheet": sheet, "row": row},
            timeout=30,
        )
        if r.status_code >= 300 or "ok" not in r.text[:200].lower():
            log.warning("Sheet log failed (%s): %s", r.status_code, r.text[:200])
    except requests.RequestException as exc:
        log.warning("Sheet log failed: %s", exc)


def gscript(cfg: dict, action: str, **payload) -> dict:
    """Call the Apps Script helper (profile docs, cover letter docs, calendar)."""
    url = cfg.get("sheet_webhook_url")
    if not url:
        raise RuntimeError("sheet_webhook_url is not set")
    r = requests.post(url, json={"secret": cfg.get("sheet_webhook_secret", ""), "action": action, **payload}, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Apps Script '{action}' returned: {r.text[:200]}")
    if not data.get("ok"):
        raise RuntimeError(f"Apps Script '{action}' failed: {data.get('error')}")
    return data


# ---------------------------------------------------------------- unsubscribe

def one_click_unsubscribe(url: str, dry_run: bool) -> tuple[bool, str]:
    """RFC 8058: a POST with this exact body, no cookies, no redirects followed
    to other sites. Only ever called with a URL from a verified header."""
    if dry_run:
        return True, "dry run"
    try:
        r = requests.post(
            url,
            data="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0 (email-triage)"},
            timeout=20,
            allow_redirects=False,
        )
        ok = r.status_code < 400
        return ok, f"HTTP {r.status_code}"
    except requests.RequestException as exc:
        return False, str(exc)[:120]


# ---------------------------------------------------------------- bills

def _load_billers() -> dict:
    try:
        with open(BILLERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_billers(data: dict) -> None:
    tmp = BILLERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, BILLERS_FILE)


def _norm(v) -> str:
    return re.sub(r"[\s\-]", "", str(v or "")).upper()


def check_bank_details(em: Email, bill: dict, dry_run: bool) -> list[str]:
    """Compare payment details with what this sender used last time.
    Returns a list of changed fields (empty if new biller or unchanged)."""
    key = em.from_domain or em.from_addr
    current = {
        "bsb": _norm(bill.get("bsb")),
        "account_number": _norm(bill.get("account_number")),
        "bpay_biller_code": _norm(bill.get("bpay_biller_code")),
    }
    current = {k: v for k, v in current.items() if v}
    billers = _load_billers()
    previous = billers.get(key, {}).get("details", {})
    changed = [k for k, v in current.items() if previous.get(k) and previous[k] != v]
    # Always remember the latest details: the first bill showing a change is the
    # one that gets flagged; if the change was genuine, later bills won't re-flag.
    if current and not dry_run:
        billers[key] = {"name": bill.get("biller"), "details": {**previous, **current}, "updated": _now()}
        _save_billers(billers)
    return changed


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "amount unknown"


def bill_task_text(em: Email, bill: dict, changed: list[str]) -> tuple[str, str]:
    biller = bill.get("biller") or em.from_name or em.from_domain
    title = f"Pay {biller} {_money(bill.get('amount'))}"
    if changed:
        title = "⚠️ CHECK BANK DETAILS - " + title
    lines = []
    if changed:
        lines.append(
            f"**Warning:** the {', '.join(c.replace('_', ' ') for c in changed)} differ from this sender's previous bill. "
            "Invoice scams work exactly like this. Confirm with the biller using a phone number you already have "
            "before paying."
        )
        lines.append("")
    if bill.get("due_date"):
        lines.append(f"Due: {bill['due_date']}")
    if bill.get("invoice_number"):
        lines.append(f"Invoice: {bill['invoice_number']}")
    if bill.get("payment_url"):
        lines.append(f"Pay online: {bill['payment_url']}")
    if bill.get("bpay_biller_code"):
        lines.append(f"BPAY biller code: {bill['bpay_biller_code']}  Ref: {bill.get('bpay_reference') or '-'}")
    if bill.get("bsb") or bill.get("account_number"):
        name = f"{bill['bank_account_name']}, " if bill.get("bank_account_name") else ""
        lines.append(
            f"Bank transfer: {name}BSB {bill.get('bsb') or '-'}, Acc {bill.get('account_number') or '-'}, "
            f"Ref: {bill.get('payment_reference') or bill.get('invoice_number') or '-'}"
        )
    if bill.get("other_methods"):
        lines.append(f"Other: {bill['other_methods']}")
    if bill.get("notes"):
        lines.append(f"Note: {bill['notes']}")
    lines.append(f"From: {em.from_name} <{em.from_addr}>")
    lines.append(f"Email: [{em.subject[:80]}]({em.link})")
    lines.append("Details read from the email by Claude. Check them against the bill before paying.")
    return title, "\n".join(lines)


def add_todoist_task(cfg: dict, title: str, description: str, due_date: str | None, urgent: bool, dry_run: bool,
                     project_id: str | None = None) -> str | None:
    payload = {
        "content": title,
        "description": description,
        "project_id": project_id or cfg["todoist_bills_project_id"],
        "priority": 4 if urgent else 3,
    }
    if due_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_date):
        payload["due_date"] = due_date
    if dry_run:
        log.info("[dry run] would add Todoist task: %s", json.dumps(payload)[:600])
        return "dry-run"
    r = requests.post(
        TODOIST_TASKS,
        headers={"Authorization": f"Bearer {cfg['todoist_api_token']}"},
        json=payload,
        timeout=20,
    )
    if r.status_code >= 300:
        log.warning("Todoist task failed (%s): %s", r.status_code, r.text[:300])
        return None
    return r.json().get("id")


# ---------------------------------------------------------------- drafts

def build_reply(em: Email, own_address: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = own_address
    msg["To"] = em.reply_to or em.from_addr  # always the original sender, never a model-chosen address
    subject = em.subject or ""
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["In-Reply-To"] = em.message_id
    refs = (em.raw_references + " " + em.message_id).strip()
    msg["References"] = refs
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(body)
    return msg.as_bytes()
