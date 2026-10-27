"""Subscriptions: keeps the Todoist "Subscriptions" project in step with
subscription emails (sign-ups, receipts, renewals, price changes, trials,
failed payments, cancellations).

The project itself is the record. Every task is read from Todoist each time,
so entries Brad adds, edits or moves by hand are matched too. Each email is
added as a comment, so every entry keeps its own history.

Card format (Brad's standard, Oct 2026):
  Title        "Service — $amount CUR"
  Description  Amount AUD: ...
               Amount USD: ...
               Frequency: ...
               Payment method: ...
               Work-use %: ...
               Notes: ...            (optional)
  Due date     annual subscriptions only: a recurring yearly due date on the
               next renewal. Monthly ones have no due date (a quarterly
               "Review monthly subscriptions" task covers them).

Sections:
  Annual / Monthly   active, by billing frequency
  Needs review       anything the agent isn't sure about (incl. weekly/quarterly)
  Cancelled          cancelled subscriptions, kept as a record
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import actions
from llm import Claude
from mail import Email

log = logging.getLogger("subscriptions")
TZ = ZoneInfo("Australia/Perth")

FREQ_SECTION = {"monthly": "Monthly", "annual": "Annual"}
REVIEW = "Needs review"
CANCELLED = "Cancelled"
DASH = " — "
NONE = "—"
TEMPLATE_KEYS = ["Amount AUD", "Amount USD", "Frequency", "Payment method", "Work-use %"]
NOTES = "Notes"


def split_title(content: str) -> tuple[str, str]:
    name, _, amount = content.partition(DASH)
    return name.strip(), amount.strip()


def money(amount, currency) -> str:
    if amount is None:
        return ""
    try:
        text = f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return ""
    return f"{text} {currency.upper()}" if currency else text


def plain(amount) -> str:
    """Amount as it goes on an 'Amount AUD/USD' line, e.g. 154.00."""
    try:
        return f"{float(amount):.2f}"
    except (TypeError, ValueError):
        return ""


def amount_key(currency) -> str:
    """Which template line an amount belongs on. Anything not USD is recorded as AUD."""
    return "Amount USD" if (currency or "").upper() == "USD" else "Amount AUD"


def price_changed(old_title_amount: str, amount, currency) -> bool | None:
    """True/False if the new charge can be compared with the amount in the task
    name; None if it can't (no number there yet, or charged in another currency,
    e.g. a USD service billed in AUD, where the figure moves with the exchange rate)."""
    try:
        new = float(amount)
    except (TypeError, ValueError):
        return None
    pairs = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)\s*([A-Z]{3})?", old_title_amount or "")
    pairs = [(float(n.replace(",", "")), c) for n, c in pairs if re.search(r"\d", n)]
    if not pairs:
        return None
    cur = (currency or "").upper()
    comparable = [n for n, c in pairs if not c or not cur or c == cur]
    if not comparable:
        return None
    return all(abs(n - new) > 0.005 for n in comparable)


def get_line(desc: str, key: str) -> str:
    for line in (desc or "").splitlines():
        m = re.match(rf"\s*{re.escape(key)}\s*:\s*(.*)$", line, re.I)
        if m:
            return m.group(1).strip()
    return ""


def set_line(desc: str, key: str, value: str) -> str:
    """Replace or add a 'Key: value' line in a task description."""
    lines = (desc or "").splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*:", line, re.I):
            lines[i] = f"{key}: {value}"
            return "\n".join(lines)
    return "\n".join(lines + [f"{key}: {value}"]).strip()


def is_blank(value: str) -> bool:
    return value.strip() in ("", NONE, "-", "___", "TBC")


def add_note(desc: str, text: str) -> str:
    """Append to the Notes line (creating it if needed)."""
    current = get_line(desc, NOTES)
    if text in current:
        return desc
    return set_line(desc, NOTES, f"{current.rstrip('.')}. {text}" if current else text)


def tidy(desc: str) -> str:
    """Put the description in template order: the five standard lines (missing
    ones filled with a dash), then Notes, then anything else that was there."""
    lines = (desc or "").splitlines()
    known = {k.lower(): k for k in TEMPLATE_KEYS + [NOTES]}
    values: dict[str, str] = {}
    extra: list[str] = []
    for line in lines:
        m = re.match(r"\s*([^:]+?)\s*:\s*(.*)$", line)
        if m and m.group(1).lower() in known and known[m.group(1).lower()] not in values:
            values[known[m.group(1).lower()]] = m.group(2).strip()
        elif line.strip():
            extra.append(line)
    out = [f"{k}: {values.get(k) or NONE}" for k in TEMPLATE_KEYS]
    if values.get(NOTES):
        out.append(f"{NOTES}: {values[NOTES]}")
    return "\n".join(out + extra)


def yearly(due: str) -> str | None:
    """Todoist due string for a yearly renewal anchored on a YYYY-MM-DD date."""
    try:
        d = date.fromisoformat(due)
    except (TypeError, ValueError):
        return None
    return f"every year starting {d.isoformat()}"


class Subscriptions:
    def __init__(self, cfg: dict, claude: Claude):
        self.cfg = cfg
        self.claude = claude
        self.project = cfg.get("todoist_subscriptions_project_id", "")
        self.live = cfg.get("assistant_live", True)

    # ---------------------------------------------------------- Todoist
    def sections(self) -> dict[str, str]:
        return actions.todoist_sections(self.cfg, self.project)

    def section_id(self, sections: dict[str, str], name: str) -> str:
        sid = sections.get(name.lower())
        if not sid:
            sid = actions.todoist_add_section(self.cfg, self.project, name)
            sections[name.lower()] = sid
            log.info("Created section %s", name)
        return sid

    def entries(self, sections: dict[str, str]) -> list[dict]:
        data = actions._todoist(self.cfg, "GET", "/tasks", params={"project_id": self.project, "limit": 200})
        tasks = data.get("results", data) if isinstance(data, dict) else data
        name_of = {v: k for k, v in sections.items()}
        out = []
        for t in tasks:
            name, amount = split_title(t.get("content", ""))
            out.append({"id": t["id"], "name": name, "amount": amount, "content": t.get("content", ""),
                        "section": name_of.get(t.get("section_id") or "", ""),
                        "description": t.get("description", ""), "due": t.get("due") or {}})
        return out

    # ---------------------------------------------------------- handling
    def alert(self, title: str, message: str, em: Email) -> None:
        self.flagged = True  # something Brad should look at: keep the email in the inbox
        actions.notify(self.cfg["notify_service"], title, message, em.link, False, style="subscription")

    flagged = False

    def handle(self, em: Email, event: str) -> str:
        self.flagged = False
        if event == "none":
            return ""
        if not self.live:
            return "[not live] subscriptions list"
        sections = self.sections()
        entries = self.entries(sections)
        s = self.claude.extract_subscription(em, event, entries)
        when = datetime.now(TZ).strftime("%d %b %Y")
        note = f"{when}: {s.get('update') or event.replace('_', ' ')} ([email]({em.link}))"
        freq = (s.get("frequency") or "").lower()
        new_amount = money(s.get("amount"), s.get("currency"))
        next_date = s.get("next_date") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.get("next_date") or "") else None

        idx = s.get("matches")
        entry = entries[idx] if isinstance(idx, int) and 0 <= idx < len(entries) else None

        # ---- not on the list yet
        if entry is None:
            name = (s.get("service") or em.from_name or em.from_domain)[:120]
            if event == "cancelled":
                target = CANCELLED
            elif event == "started" and freq in FREQ_SECTION:
                target = FREQ_SECTION[freq]
            else:
                target = REVIEW  # a charge or notice for something not on the list: let Brad decide
            desc = ""
            if plain(s.get("amount")):
                desc = set_line(desc, amount_key(s.get("currency")), plain(s.get("amount")))
            if freq:
                desc = set_line(desc, "Frequency", freq.capitalize())
            if s.get("payment_method"):
                desc = set_line(desc, "Payment method", s["payment_method"])
            desc = set_line(desc, "Work-use %", "TBC")
            due_string = due_date = None
            if event == "trial_ending" and next_date:
                desc = add_note(desc, f"Trial ends {next_date}")
                due_date = next_date
            elif freq == "annual" and next_date and target != CANCELLED:
                due_string = yearly(next_date)
            desc = add_note(desc, f"Added by email triage, {when}")
            desc = tidy(desc)
            title = f"{name}{DASH}{new_amount}" if new_amount else name
            task_id = actions.todoist_create(self.cfg, title, desc, self.project, self.section_id(sections, target),
                                             due_date, due_string)
            actions.todoist_comment(self.cfg, task_id, note)
            log.info("Subscription added: %s -> %s", title, target)
            if target == REVIEW and event == "charged":
                self.alert("💳 Unlisted subscription charge", f"{title}: not on your Subscriptions list.", em)
            return f"Subscriptions: added '{title}' to {target}"

        # ---- already on the list
        name = entry["name"]
        changes: dict = {}
        desc = entry["description"]
        result = f"Subscriptions: noted on '{name}'"
        is_annual = entry["section"] == "annual" or freq == "annual" or get_line(desc, "Frequency").lower() == "annual"
        recurring = bool(entry["due"].get("is_recurring"))

        changed = price_changed(entry["amount"], s.get("amount"), s.get("currency")) if new_amount else False
        if event in ("charged", "price_change", "started") and new_amount:
            if changed:
                changes["content"] = f"{name}{DASH}{new_amount}"
                desc = set_line(desc, amount_key(s.get("currency")), plain(s.get("amount")))
                desc = add_note(desc, f"Previous amount {entry['amount']} (to {when})")
                self.alert("💲 Subscription price change", f"{name}: {entry['amount']} → {new_amount}", em)
                result = f"Subscriptions: '{name}' amount now {new_amount} (was {entry['amount']})"
            elif changed is None and "$" not in entry["amount"]:
                changes["content"] = f"{name}{DASH}{new_amount}"  # fill in a missing amount quietly
                desc = set_line(desc, amount_key(s.get("currency")), plain(s.get("amount")))
                result = f"Subscriptions: '{name}' amount recorded as {new_amount}"
            elif is_blank(get_line(desc, amount_key(s.get("currency")))):
                desc = set_line(desc, amount_key(s.get("currency")), plain(s.get("amount")))
        if freq and is_blank(get_line(desc, "Frequency")):
            desc = set_line(desc, "Frequency", freq.capitalize())
        if s.get("payment_method"):
            desc = set_line(desc, "Payment method", s["payment_method"])

        # due dates: annual renewals carry a recurring yearly due date on the next renewal
        if is_annual and next_date and event in ("charged", "renewal_upcoming", "started") \
                and entry["section"] != CANCELLED.lower():
            if entry["due"].get("date", "")[:10] != next_date or not recurring:
                changes["due_string"] = yearly(next_date)
        if event == "trial_ending" and next_date:
            desc = add_note(desc, f"Trial ends {next_date}")
            if not recurring:  # never overwrite a renewal date with a one-off trial date
                changes["due_date"] = next_date

        desc = tidy(desc)
        if desc != entry["description"]:
            changes["description"] = desc
        if changes:
            actions.todoist_update(self.cfg, entry["id"], **changes)
        actions.todoist_comment(self.cfg, entry["id"], note)

        # ---- moves between sections
        target = None
        if event == "cancelled" and entry["section"] != CANCELLED.lower():
            target = CANCELLED
        elif entry["section"] == CANCELLED.lower() and event == "charged":
            target = REVIEW  # charged after cancelling: worth a look
            self.alert("⚠️ Charged after cancelling", f"{name} charged you {new_amount or 'again'}.", em)
        elif entry["section"] == CANCELLED.lower() and event == "started":
            target = FREQ_SECTION.get(freq, REVIEW)  # resubscribed
        if target:
            actions.todoist_move(self.cfg, entry["id"], self.section_id(sections, target))
            if target == CANCELLED and recurring:
                actions.todoist_update(self.cfg, entry["id"], due_string="no date")  # cancelled cards have no due date
            log.info("Subscription moved: %s -> %s", name, target)
            result = f"Subscriptions: moved '{name}' to {target}"
        return result
