"""Gmail IMAP access and message parsing.

Everything that touches the mailbox lives here. The agent only ever:
  - reads messages (BODY.PEEK, so nothing is marked as read),
  - adds Gmail labels,
  - archives a message (removes the Inbox label; it stays in All Mail),
  - moves a message to Spam,
  - appends a draft to Drafts.
It never sends, deletes or expunges anything.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import io
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

log = logging.getLogger("mail")

IMAP_HOST = "imap.gmail.com"
MAX_BODY_CHARS = 12000
MAX_PDF_CHARS = 6000
MAX_HTML_CHARS = 25000


@dataclass
class Attachment:
    filename: str
    content_type: str
    text: str = ""  # extracted text for PDFs


@dataclass
class Email:
    uid: int
    message_id: str
    from_name: str
    from_addr: str
    reply_to: str
    to: str
    subject: str
    date: str
    body: str
    headers: dict = field(default_factory=dict)
    attachments: list = field(default_factory=list)
    gmail_labels: str = ""
    raw_references: str = ""
    html_text: str = ""  # HTML part as text with link targets kept, for job alerts

    @property
    def from_domain(self) -> str:
        return self.from_addr.rsplit("@", 1)[-1].lower() if "@" in self.from_addr else ""

    @property
    def link(self) -> str:
        mid = self.message_id.strip("<>")
        return "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{mid}", safe="")


# ---------------------------------------------------------------- parsing

def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        # keep link targets: payment links matter for bills
        for a in soup.find_all("a"):
            href = a.get("href", "")
            if href.startswith("http") and href not in a.get_text():
                a.append(f" [{href}]")
        text = soup.get_text("\n")
    except Exception:  # pragma: no cover - bs4 missing
        text = re.sub(r"<[^>]+>", " ", html)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:5]:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) > MAX_PDF_CHARS:
                break
        return "\n".join(parts)[:MAX_PDF_CHARS]
    except Exception as exc:
        log.debug("PDF text extraction failed: %s", exc)
        return ""


def parse_message(uid: int, raw: bytes, gmail_labels: str = "") -> Email:
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    name, addr = email.utils.parseaddr(str(msg.get("From", "")))
    plain, html = None, None
    attachments: list[Attachment] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        fname = part.get_filename()
        if fname or disp == "attachment":
            att = Attachment(filename=fname or "attachment", content_type=ctype)
            if ctype == "application/pdf" or (fname or "").lower().endswith(".pdf"):
                att.text = _pdf_text(part.get_payload(decode=True) or b"")
            attachments.append(att)
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "text/html" and html is None:
            html = content

    body = plain if plain and plain.strip() else _html_to_text(html or "")
    # plain text parts often drop link targets that the HTML has; append them for bills
    if plain and html:
        links = re.findall(r'href=["\'](https?://[^"\']+)', html)
        pay_links = [l for l in links if re.search(r"pay|bill|invoice|bpay|account", l, re.I)]
        if pay_links:
            body += "\n\nLinks in the HTML version:\n" + "\n".join(dict.fromkeys(pay_links[:10]))

    keep = [
        "From", "Reply-To", "To", "Cc", "Subject", "Date", "Message-ID",
        "List-Unsubscribe", "List-Unsubscribe-Post", "List-Id", "Precedence",
        "Authentication-Results", "Auto-Submitted", "References", "In-Reply-To",
    ]
    headers = {}
    for h in keep:
        vals = msg.get_all(h)
        if vals:
            headers[h] = " | ".join(str(v) for v in vals)

    return Email(
        uid=uid,
        message_id=str(msg.get("Message-ID", "")).strip() or f"<uid-{uid}@local>",
        from_name=name,
        from_addr=addr.lower(),
        reply_to=email.utils.parseaddr(str(msg.get("Reply-To", "")))[1].lower() or addr.lower(),
        to=str(msg.get("To", "")),
        subject=str(msg.get("Subject", "")),
        date=str(msg.get("Date", "")),
        body=(body or "")[:MAX_BODY_CHARS],
        headers=headers,
        attachments=attachments,
        gmail_labels=gmail_labels,
        raw_references=str(msg.get("References", "")),
        html_text=_html_to_text(html)[:MAX_HTML_CHARS] if html else "",
    )


# ---------------------------------------------------------------- unsubscribe safety

def one_click_unsubscribe_url(em: Email) -> tuple[str | None, str]:
    """Return (url, reason). A URL is only returned when the sender supports
    RFC 8058 one-click unsubscribe AND Gmail verified the message's DKIM
    signature for the sender's own domain. That is the same bar Gmail uses
    for its own Unsubscribe button. Links in the email body are never used."""
    lu = em.headers.get("List-Unsubscribe", "")
    lup = em.headers.get("List-Unsubscribe-Post", "")
    if not lu:
        return None, "no unsubscribe header"
    if "list-unsubscribe=one-click" not in lup.lower():
        return None, "sender has no one-click unsubscribe"
    urls = re.findall(r"<(https://[^>]+)>", lu)
    if not urls:
        return None, "unsubscribe header has no https link"

    auth = em.headers.get("Authentication-Results", "").lower()
    domain = em.from_domain
    dkim_domains = re.findall(r"dkim=pass[^;]*?header\.(?:i|d)=@?([a-z0-9.\-]+)", auth)
    aligned = any(d == domain or domain.endswith("." + d) or d.endswith("." + domain) for d in dkim_domains)
    if not aligned:
        return None, "sender identity not verified (no aligned DKIM pass)"
    return urls[0], "verified one-click"


# ---------------------------------------------------------------- IMAP client

class Mailbox:
    def __init__(self, address: str, app_password: str, dry_run: bool):
        self.address = address
        self.password = app_password
        self.dry_run = dry_run
        self.conn: imaplib.IMAP4_SSL | None = None
        self._special: dict[str, str] = {}

    # -- connection
    def connect(self):
        if self.conn is not None:
            try:
                self.conn.noop()
                return
            except Exception:
                self.conn = None
        conn = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=60)
        conn.login(self.address, self.password)
        self.conn = conn
        self._special = self._find_special_folders()
        self.conn.select("INBOX")

    def close(self):
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def _find_special_folders(self) -> dict[str, str]:
        found = {"spam": "[Gmail]/Spam", "drafts": "[Gmail]/Drafts", "all": "[Gmail]/All Mail"}
        typ, data = self.conn.list()
        for line in data or []:
            s = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
            m = re.match(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$', s)
            if not m:
                continue
            name = m.group("name").strip().strip('"')
            flags = m.group("flags")
            if "\\Junk" in flags:
                found["spam"] = name
            if "\\Drafts" in flags:
                found["drafts"] = name
            if "\\All" in flags:
                found["all"] = name
        return found

    @staticmethod
    def _q(folder: str) -> str:
        return '"' + folder.replace('"', '\\"') + '"'

    # -- reading
    def inbox_status(self) -> tuple[int, int]:
        """(uidvalidity, uidnext) for INBOX."""
        self.connect()
        typ, data = self.conn.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        s = data[0].decode()
        uv = int(re.search(r"UIDVALIDITY (\d+)", s).group(1))
        un = int(re.search(r"UIDNEXT (\d+)", s).group(1))
        return uv, un

    def uids_after(self, last_uid: int) -> list[int]:
        self.connect()
        self.conn.select("INBOX")
        typ, data = self.conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        uids = [int(x) for x in (data[0] or b"").split()]
        # "N:*" always returns the highest UID even if it is <= last_uid
        return sorted(u for u in uids if u > last_uid)

    def uids_since(self, day: str) -> list[int]:
        """Inbox UIDs received on or after `day` (IMAP date, e.g. 19-Oct-2026)."""
        self.connect()
        self.conn.select("INBOX")
        typ, data = self.conn.uid("SEARCH", None, f"SINCE {day}")
        return sorted(int(x) for x in (data[0] or b"").split())

    def fetch(self, uid: int) -> Email | None:
        self.connect()
        self.conn.select("INBOX")
        typ, data = self.conn.uid("FETCH", str(uid), "(X-GM-LABELS BODY.PEEK[])")
        raw, meta = None, ""
        for item in data or []:
            if isinstance(item, tuple):
                meta = item[0].decode(errors="replace")
                raw = item[1]
        if raw is None:
            return None
        labels = ""
        m = re.search(r"X-GM-LABELS \((.*?)\)", meta)
        if m:
            labels = m.group(1)
        return parse_message(uid, raw, labels)

    # -- changes (all skipped in dry run)
    def add_labels(self, uid: int, labels: list[str]):
        if not labels:
            return
        if self.dry_run:
            log.info("[dry run] would label UID %s with %s", uid, labels)
            return
        self.connect()
        self.conn.select("INBOX")
        quoted = " ".join(self._q(l) for l in labels)
        typ, data = self.conn.uid("STORE", str(uid), "+X-GM-LABELS", f"({quoted})")
        if typ != "OK":
            log.warning("Labelling UID %s failed: %s", uid, data)

    def archive(self, uid: int):
        """Gmail archive: drop the Inbox label. Nothing is deleted."""
        if self.dry_run:
            log.info("[dry run] would archive UID %s", uid)
            return
        self.connect()
        self.conn.select("INBOX")
        typ, data = self.conn.uid("STORE", str(uid), "-X-GM-LABELS", "(\\Inbox)")
        if typ != "OK":
            log.warning("Archiving UID %s failed: %s", uid, data)

    def inbox_uid(self, message_id: str) -> int | None:
        """UID of this message if it is (still) in the inbox, else None."""
        self.connect()
        self.conn.select("INBOX")
        mid = message_id.strip()
        if not mid or '"' in mid:
            return None
        typ, data = self.conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{mid}"')
        uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
        return uids[-1] if uids else None

    def archive_by_move(self, uid: int):
        """Fallback archive: move the message to All Mail, which drops the Inbox label."""
        if self.dry_run:
            return
        self.connect()
        self.conn.select("INBOX")
        typ, data = self.conn.uid("MOVE", str(uid), self._q(self._special.get("all", "[Gmail]/All Mail")))
        if typ != "OK":
            log.warning("Archive (move) of UID %s failed: %s", uid, data)

    def move_to_spam(self, uid: int):
        if self.dry_run:
            log.info("[dry run] would move UID %s to Spam", uid)
            return
        self.connect()
        self.conn.select("INBOX")
        typ, data = self.conn.uid("MOVE", str(uid), self._q(self._special["spam"]))
        if typ != "OK":
            log.warning("Moving UID %s to spam failed: %s", uid, data)

    def save_draft(self, raw_message: bytes, label: str | None = None) -> bool:
        if self.dry_run:
            log.info("[dry run] would save a draft (%d bytes)", len(raw_message))
            return True
        self.connect()
        folder = self._special["drafts"]
        typ, data = self.conn.append(
            self._q(folder), "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()), raw_message
        )
        if typ != "OK":
            log.warning("Saving draft failed: %s", data)
            return False
        if label:
            try:
                m = re.search(rb"APPENDUID \d+ (\d+)", data[0] if data else b"")
                if m:
                    self.conn.select(self._q(folder))
                    self.conn.uid("STORE", m.group(1).decode(), "+X-GM-LABELS", f"({self._q(label)})")
            except Exception as exc:
                log.debug("Could not label draft: %s", exc)
            finally:
                self.conn.select("INBOX")
        return True
