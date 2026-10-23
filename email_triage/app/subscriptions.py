"""Subscriptions: keeps the Todoist "Subscriptions" project in step with
subscription emails (sign-ups, receipts, renewals, price changes, trials,
failed payments, cancellations).

The project itself is the record. Every task is read from Todoist each time,
so entries Brad adds, edits or moves by hand are matched too. Task names are
"Service — $amount CUR"; descriptions hold "Key: value" lines (Frequency,
Payment method, Next renewal, Trial ends, ...). Each email is added as a
comment, so every entry keeps its own history.

Sections:
  Monthly / Quarterly / Annual   active, by billing frequency
  Needs review                   anything the agent isn't sure about
  Cancelled                      cancelled subscriptions, kept as a record
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import actions
from llm import Claude
from mail import Email

log = logging.getLogger("subscriptions")
TZ = ZoneInfo("Australia/Perth")

FREQ_SECTION = {"weekly": "Weekly", "monthly": "Monthly", "quarterly": "Quarterly", "annual": "Annual"}
REVIEW = "Needs review"
CANCELLED = "Cancelled"
DASH = " — "


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


def set_line(desc: str, key: str, value: str) -> str:
    """Replace or add a 'Key: value' line in a task description."""
    lines = (desc or "").splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*:", line, re.I):
            lines[i] = f"{key}: {value}"
            return "\n".join(lines)
    return "\n".join(lines + [f"{key}: {value}"]).strip()


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
                        "description": t.get("description", "")})
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
            if freq:
                desc = set_line(desc, "Frequency", freq.capitalize())
            if s.get("payment_method"):
                desc = set_line(desc, "Payment method", s["payment_method"])
            if s.get("next_date"):
                desc = set_line(desc, "Trial ends" if event == "trial_ending" else "Next renewal", s["next_date"])
            desc = set_line(desc, "Added by", f"email triage, {when}")
            title = f"{name}{DASH}{new_amount}" if new_amount else name
            task_id = actions.todoist_create(self.cfg, title, desc, self.project, self.section_id(sections, target),
                                             s.get("next_date") if event == "trial_ending" else None)
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

        changed = price_changed(entry["amount"], s.get("amount"), s.get("currency")) if new_amount else False
        if event in ("charged", "price_change", "started") and new_amount:
            if changed:
                changes["content"] = f"{name}{DASH}{new_amount}"
                desc = set_line(desc, "Previous amount", entry["amount"])
                self.alert("💲 Subscription price change", f"{name}: {entry['amount']} → {new_amount}", em)
                result = f"Subscriptions: '{name}' amount now {new_amount} (was {entry['amount']})"
            elif changed is None and "$" not in entry["amount"]:
                changes["content"] = f"{name}{DASH}{new_amount}"  # fill in a missing amount quietly
                result = f"Subscriptions: '{name}' amount recorded as {new_amount}"
        if s.get("payment_method"):
            desc = set_line(desc, "Payment method", s["payment_method"])
        if s.get("next_date") and event in ("charged", "renewal_upcoming", "started"):
            desc = set_line(desc, "Next renewal", s["next_date"])
        if s.get("next_date") and event == "trial_ending":
            desc = set_line(desc, "Trial ends", s["next_date"])
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s["next_date"]):
                changes["due_date"] = s["next_date"]
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
            log.info("Subscription moved: %s -> %s", name, target)
            result = f"Subscriptions: moved '{name}' to {target}"
        return result
