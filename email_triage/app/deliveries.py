"""Deliveries: keeps the Todoist "Deliveries" board in step with order and
parcel emails.

The board itself is the record. Open cards are read from Todoist each time,
so cards Brad adds or moves by hand (or imported ones) are matched too.
Each card's description holds its identifiers on "Key: value" lines; every
email update is added as a comment, so the card keeps its own history.

Columns (sections) by stage:
  ordered                     -> Ordered
  shipped, out_for_delivery   -> On it's way
  delivered                   -> Arrived
  problem                     -> Follow-ups / Returns
  cancelled, returned         -> Cancelled / Returned
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import actions
from llm import Claude
from mail import Email

log = logging.getLogger("deliveries")
TZ = ZoneInfo("Australia/Perth")

SECTION_FOR = {
    "ordered": "ordered",
    "shipped": "on its way",
    "out_for_delivery": "on its way",
    "delivered": "arrived",
    "problem": "follow-ups / returns",
    "cancelled": "cancelled / returned",
    "returned": "cancelled / returned",
}
# Progress order; a late "order confirmed" email never pulls a card backwards.
RANK = {"ordered": 1, "shipped": 2, "out_for_delivery": 3, "delivered": 4}
STAGE_OF_SECTION = {"ordered": "ordered", "on its way": "shipped", "arrived": "delivered",
                    "follow-ups / returns": "problem", "cancelled / returned": "cancelled"}
FIELDS = [("order_number", "Order"), ("carrier", "Carrier"), ("tracking_number", "Tracking"),
          ("tracking_url", "Track"), ("eta", "ETA")]
LABEL_TO_FIELD = {label.lower(): key for key, label in FIELDS}
DISPLAY = {"ordered": "Ordered", "on its way": "On it's way", "arrived": "Arrived",
           "follow-ups / returns": "Follow-ups / Returns", "cancelled / returned": "Cancelled / Returned"}


def parse_description(desc: str) -> tuple[dict, list[str]]:
    """Return (known fields, other lines Brad may have written)."""
    fields, other = {}, []
    for line in (desc or "").splitlines():
        m = re.match(r"\s*(\w+):\s*(.+)$", line)
        if m and m.group(1).lower() in LABEL_TO_FIELD:
            fields[LABEL_TO_FIELD[m.group(1).lower()]] = m.group(2).strip()
        elif line.strip() and not line.startswith("_Updates from email"):
            other.append(line)
    return fields, other


def build_description(fields: dict, other: list[str]) -> str:
    lines = [f"{label}: {fields[key]}" for key, label in FIELDS if fields.get(key)]
    if other:
        lines += [""] + other
    lines += ["", "_Updates from email are added as comments._"]
    return "\n".join(lines).strip()


class Deliveries:
    def __init__(self, cfg: dict, claude: Claude):
        self.cfg = cfg
        self.claude = claude
        self.project = cfg.get("todoist_deliveries_project_id", "")
        self.live = cfg.get("assistant_live", True)
        self._sections: dict[str, str] = {}

    def sections(self) -> dict[str, str]:
        if not self._sections:
            self._sections = actions.todoist_sections(self.cfg, self.project)
        return self._sections

    def open_cards(self) -> list[dict]:
        data = actions._todoist(self.cfg, "GET", "/tasks", params={"project_id": self.project, "limit": 200})
        tasks = data.get("results", data) if isinstance(data, dict) else data
        name_of = {v: k for k, v in self.sections().items()}
        cards = []
        for t in tasks:
            section = name_of.get(t.get("section_id") or "", "")
            if section in ("information", "cancelled / returned"):
                continue
            fields, _ = parse_description(t.get("description", ""))
            retailer, _, item = t.get("content", "").partition(": ")
            cards.append({**fields, "id": t["id"], "retailer": retailer, "item": item or t.get("content", ""),
                          "section": section, "stage": STAGE_OF_SECTION.get(section, ""),
                          "description": t.get("description", "")})
        return cards

    def handle(self, em: Email, stage: str) -> str:
        if stage not in SECTION_FOR:
            return ""
        if not self.live:
            return "[not live] delivery board"
        cards = self.open_cards()
        d = self.claude.extract_delivery(em, cards)
        section_id = self.sections().get(SECTION_FOR[stage])
        when = datetime.now(TZ).strftime("%d %b")
        note = f"{when}: {d.get('update') or stage.replace('_', ' ')} ([email]({em.link}))"
        new = {k: d.get(k) for k, _ in FIELDS if d.get(k)}

        idx = d.get("matches")
        card = cards[idx] if isinstance(idx, int) and 0 <= idx < len(cards) else None
        if card is None:
            title = f"{d.get('retailer') or em.from_name or em.from_domain}: {d.get('item') or 'order'}"[:200]
            task_id = actions.todoist_create(self.cfg, title, build_description(new, []), self.project, section_id)
            actions.todoist_comment(self.cfg, task_id, note)
            log.info("Delivery card added: %s (%s)", title, stage)
            return f"Deliveries: added '{title}' to {DISPLAY[SECTION_FOR[stage]]}"

        fields, other = parse_description(card["description"])
        merged = {**fields, **new}
        if merged != fields:
            actions.todoist_update(self.cfg, card["id"], description=build_description(merged, other))
        actions.todoist_comment(self.cfg, card["id"], note)
        current = card.get("stage", "")
        # problems, cancellations and returns always move; normal progress only moves forwards
        forward = not (stage in RANK and current in RANK) or RANK[stage] > RANK[current]
        title = f"{card['retailer']}: {card['item']}".strip(": ")
        if section_id and forward and card.get("section") != SECTION_FOR[stage]:
            actions.todoist_move(self.cfg, card["id"], section_id)
            log.info("Delivery card moved: %s -> %s", title, SECTION_FOR[stage])
            return f"Deliveries: moved '{title}' to {DISPLAY[SECTION_FOR[stage]]}"
        return f"Deliveries: updated '{title}'"
