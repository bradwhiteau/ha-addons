"""Career Counsellor: turns SEEK job alerts into a short list of roles worth
Brad's attention, asks him on his phone, and on "Interested" drafts a cover
letter, adds a to-do and a calendar reminder.

Safety: job ads are untrusted text. The model only fills in structured fields;
this code decides what happens. Pages are only fetched from seek.com.au.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

import actions
from llm import Claude, SAFETY
from mail import Email

log = logging.getLogger("career")
TZ = ZoneInfo("Australia/Perth")
JOBS_FILE = "/data/jobs.json"
PROFILE_TTL = 30 * 60
RESUME_PDF = "https://drive.google.com/file/d/18g_MTJoAQTvqshqMMYloP-tilCZSFYz9/view"
MAX_PROMPTS_PER_ALERT = 5

EXTRACT_TOOL = {
    "name": "record_jobs",
    "description": "Record every job listing in this job alert email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "maxItems": 15,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "company": {"type": "string"},
                        "location": {"type": "string"},
                        "pay_or_type": {"type": "string", "description": "Salary/rate and contract/permanent/full-time if shown, else empty."},
                        "summary": {"type": "string", "description": "The listing's teaser text, as shown."},
                        "url": {"type": "string", "description": "The link for this job (the URL in [brackets] after its title), exactly as written."},
                    },
                    "required": ["title", "company", "location", "pay_or_type", "summary", "url"],
                },
            }
        },
        "required": ["jobs"],
    },
}

ASSESS_TOOL = {
    "name": "record_assessments",
    "description": "Record how well each job fits Brad.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "The job's number in the list."},
                        "fit": {"type": "string", "enum": ["strong", "possible", "no"]},
                        "why": {"type": "string", "description": "One short sentence: the main reason for this rating."},
                        "pitch": {"type": "string", "description": "Phone-sized, under 120 characters: what the role is and why it might suit him."},
                    },
                    "required": ["index", "fit", "why", "pitch"],
                },
            }
        },
        "required": ["assessments"],
    },
}

LETTER_TOOL = {
    "name": "record_cover_letter",
    "description": "Record the cover letter draft.",
    "input_schema": {
        "type": "object",
        "properties": {
            "letter": {"type": "string", "description": "The full letter text, from the salutation to the sign-off."},
            "wins_used": {"type": "string", "description": "Which 1-2 achievements from the dossier the letter leans on."},
            "notes_for_brad": {"type": "string", "description": "One or two lines: anything to check or add before sending (e.g. 'full ad not available, written from the alert summary')."},
        },
        "required": ["letter", "wins_used", "notes_for_brad"],
    },
}


def job_key(job: dict) -> str:
    m = re.search(r"/job/(\d+)", job.get("url", ""))
    if m:
        return "seek-" + m.group(1)
    raw = f"{job.get('title','').lower()}|{job.get('company','').lower()}"
    return "h-" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def _is_seek(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "seek.com.au" or host.endswith(".seek.com.au"))


def fetch_ad(url: str) -> tuple[str, str]:
    """Return (ad_text, final_url). Empty text if the ad couldn't be read."""
    if not _is_seek(url):
        return "", url
    try:
        r = requests.get(
            url, timeout=25, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
                     "Accept-Language": "en-AU,en;q=0.9"},
        )
    except requests.RequestException as exc:
        log.info("Could not fetch job ad: %s", exc)
        return "", url
    final = r.url
    if r.status_code >= 400 or not _is_seek(final):
        return "", final
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(r.text, "html.parser")
        node = soup.select_one('[data-automation="jobAdDetails"]')
        text = node.get_text("\n", strip=True) if node else ""
        if len(text) < 300:
            for s in soup.find_all("script", type="application/ld+json"):
                try:
                    d = json.loads(s.string or "")
                except ValueError:
                    continue
                for item in d if isinstance(d, list) else [d]:
                    if isinstance(item, dict) and item.get("description"):
                        text = BeautifulSoup(item["description"], "html.parser").get_text("\n", strip=True)
        return (text[:9000] if len(text) >= 300 else ""), final
    except Exception as exc:
        log.info("Could not parse job ad: %s", exc)
        return "", final


class Career:
    def __init__(self, cfg: dict, claude: Claude):
        self.cfg = cfg
        self.claude = claude
        self.live = cfg.get("assistant_live", True)
        self.lock = threading.Lock()
        self._profile: dict | None = None
        self._profile_at = 0.0

    # ---------------------------------------------------------- storage
    def _load(self) -> dict:
        try:
            with open(JOBS_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        # keep the store small: drop entries older than 60 days
        cutoff = (datetime.now(TZ) - timedelta(days=60)).isoformat()
        data = {k: v for k, v in data.items() if v.get("seen", "") >= cutoff}
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, JOBS_FILE)

    # ---------------------------------------------------------- profile
    def profile(self) -> dict:
        if self._profile and time.time() - self._profile_at < PROFILE_TTL:
            return self._profile
        data = actions.gscript(self.cfg, "get_profile")
        self._profile = {"criteria": data.get("criteria", ""), "dossier": data.get("dossier", "")}
        self._profile_at = time.time()
        return self._profile

    # ---------------------------------------------------------- alert handling
    def handle_alert(self, em: Email) -> str:
        source = em.html_text or em.body
        system = (
            "You read job alert emails for Brad White and list every job in them. "
            "Links appear as [https://...] after the text they belong to. " + SAFETY
        )
        extracted = self.claude._call(
            self.claude.triage_model, system,
            f"<email>\nSubject: {em.subject}\n\n{source}\n</email>\n\nList every job in this alert.",
            EXTRACT_TOOL, 3000,
        ).get("jobs", [])
        if not extracted:
            return "job alert: no jobs found"

        with self.lock:
            store = self._load()
        new_jobs = [j for j in extracted if job_key(j) not in store]
        if not new_jobs:
            return f"job alert: {len(extracted)} jobs, all seen before"

        prof = self.profile()
        listing = "\n\n".join(
            f"<job index=\"{i}\">\nTitle: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
            f"Pay/type: {j['pay_or_type']}\nSummary: {j['summary']}\n</job>"
            for i, j in enumerate(new_jobs)
        )
        system = (
            "You are Brad White's career counsellor. Judge each job against his criteria and background. "
            "'strong' = clearly the kind of role he wants; 'possible' = worth a look but something is off or unclear; "
            "'no' = wrong kind of role, or hits a deal-breaker. Criteria marked [confirm] are tentative: weigh them lightly. "
            "Blank criteria mean no preference, so don't mark a job down for them. Be selective: most alert jobs are not a fit. "
            + SAFETY
        )
        user = (
            f"<criteria>\n{prof['criteria']}\n</criteria>\n\n<background>\n{prof['dossier'][:12000]}\n</background>\n\n"
            f"<jobs>\n{listing}\n</jobs>\n\nAssess every job."
        )
        assessed = {a["index"]: a for a in self.claude._call(self.claude.reasoning_model, system, user, ASSESS_TOOL, 2500).get("assessments", [])}

        now = datetime.now(TZ)
        prompted = 0
        counts = {"strong": 0, "possible": 0, "no": 0}
        for i, job in enumerate(new_jobs):
            a = assessed.get(i, {"fit": "no", "why": "not assessed", "pitch": ""})
            fit = a.get("fit", "no")
            counts[fit] = counts.get(fit, 0) + 1
            key = job_key(job)
            rec = {**job, "key": key, "fit": fit, "why": a.get("why", ""), "pitch": a.get("pitch", ""),
                   "seen": now.isoformat(), "alert_link": em.link, "status": "skipped" if fit == "no" else "asked"}
            call = "-"
            if fit in ("strong", "possible"):
                if prompted < MAX_PROMPTS_PER_ALERT:
                    self.ask(rec)
                    prompted += 1
                    call = "asked on phone" if self.live else "[not live] would ask on phone"
                else:
                    rec["status"] = "not asked (too many in one alert)"
                    call = "not asked: too many in one alert, see log"
            with self.lock:
                store = self._load()
                store[key] = rec
                self._save(store)
            actions.sheet_log(self.cfg, "Job matches", [
                now.strftime("%Y-%m-%d %H:%M"), job["title"], job["company"], job["location"], job["pay_or_type"],
                fit, a.get("why", ""), call, "", job.get("url", ""), em.link,
            ])
        return f"job alert: {len(new_jobs)} new ({counts['strong']} strong, {counts['possible']} possible)"

    def reask_pending(self) -> int:
        """Send the phone prompt again for every role still waiting on an answer."""
        with self.lock:
            pending = [r for r in self._load().values() if r.get("status") == "asked"]
        for rec in sorted(pending, key=lambda r: (r.get("fit") != "strong", r.get("seen", ""))):
            self.ask(rec)
        log.info("Re-sent %s job prompt(s)", len(pending))
        return len(pending)

    def retry_failed(self) -> int:
        """Redo the cover letter for every 'Interested' role whose drafting failed."""
        with self.lock:
            failed = [r for r in self._load().values() if r.get("status") == "failed" and r.get("short")]
        for rec in failed:
            self.handle_action(f"CAREER_YES_{rec['short']}")
        log.info("Retried %s failed application(s)", len(failed))
        return len(failed)

    def ask(self, rec: dict) -> None:
        short = hashlib.sha1(rec["key"].encode()).hexdigest()[:10]
        rec["short"] = short
        star = "⭐ " if rec["fit"] == "strong" else ""
        actions.notify(
            self.cfg["notify_service"],
            f"{star}{rec['title']} - {rec['company']}"[:80],
            f"{rec['pitch'] or rec['why']}\n{rec['location']} {rec['pay_or_type']}".strip(),
            rec.get("url"),
            not self.live,
            extra={
                "tag": f"job_{short}",
                "actions": [
                    {"action": f"CAREER_YES_{short}", "title": "Interested"},
                    {"action": f"CAREER_NO_{short}", "title": "Not for me"},
                ],
            },
            channel="Job matches",
            style="job",
        )

    # ---------------------------------------------------------- button presses
    def handle_action(self, action: str) -> None:
        m = re.fullmatch(r"CAREER_(YES|NO)_([0-9a-f]{10})", action or "")
        if not m:
            return
        choice, short = m.groups()
        with self.lock:
            store = self._load()
            rec = next((r for r in store.values() if r.get("short") == short), None)
            if rec is None:
                log.info("Button press for an unknown job (%s)", short)
                return
            if rec.get("status") not in ("asked", "failed"):
                log.info("Ignoring repeat button press for %s (%s)", rec["title"], rec.get("status"))
                return
            rec["status"] = "working" if choice == "YES" else "declined"
            store[rec["key"]] = rec
            self._save(store)

        now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        if choice == "NO":
            actions.sheet_log(self.cfg, "Job matches", [
                now, rec["title"], rec["company"], rec["location"], rec["pay_or_type"], rec["fit"], rec["why"],
                "Not for me", "", rec.get("url", ""), rec.get("alert_link", ""),
            ])
            return

        try:
            doc_url = self.prepare_application(rec)
            status = "Interested: cover letter drafted"
        except Exception as exc:
            log.exception("Preparing application failed: %s", exc)
            doc_url = ""
            status = f"Interested: drafting FAILED ({str(exc)[:80]})"
            actions.notify(self.cfg["notify_service"], "Cover letter didn't work",
                           f"{rec['title']} - {rec['company']}: {str(exc)[:150]}", rec.get("url"), not self.live,
                           channel="Job matches", style="problem")
        with self.lock:
            store = self._load()
            rec["status"] = "drafted" if doc_url else "failed"
            store[rec["key"]] = rec
            self._save(store)
        actions.sheet_log(self.cfg, "Job matches", [
            now, rec["title"], rec["company"], rec["location"], rec["pay_or_type"], rec["fit"], rec["why"],
            status, doc_url, rec.get("url", ""), rec.get("alert_link", ""),
        ])

    def prepare_application(self, rec: dict) -> str:
        prof = self.profile()
        ad_text, final_url = fetch_ad(rec.get("url", ""))
        if ad_text:
            jd = f"<job_ad>\n{ad_text}\n</job_ad>"
        else:
            jd = (f"<job_alert_summary>\nTitle: {rec['title']}\nCompany: {rec['company']}\nLocation: {rec['location']}\n"
                  f"Pay/type: {rec['pay_or_type']}\nSummary: {rec['summary']}\n</job_alert_summary>\n"
                  "(The full job ad could not be fetched.)")
        system = (
            "You write cover letters for Brad White. Rules: open with 'To whom it may concern,'. "
            "Professional, evidence-based, and mirror the specific language of the job ad while staying authentic to Brad's voice. "
            "Lean on the 1-2 achievements from his dossier that best match this role (use its modular blocks as a guide). "
            "Only state facts that appear in his dossier; never invent experience, numbers or employers. "
            "About 300-400 words. Sign off exactly:\nRegards,\nBrad White\n"
            "If the full ad wasn't available, keep role-specific claims modest and say so in notes_for_brad. " + SAFETY
        )
        user = (
            f"<dossier>\n{prof['dossier']}\n</dossier>\n\n<criteria>\n{prof['criteria']}\n</criteria>\n\n"
            f"Role: {rec['title']} at {rec['company']} ({rec['location']})\n\n{jd}\n\nWrite the cover letter."
        )
        out = self.claude._call(self.claude.reasoning_model, system, user, LETTER_TOOL, 2500)
        title = f"Cover Letter - {rec['title']} - {rec['company']} - Brad White (draft)"
        if not self.live:
            log.info("[not live] would create doc '%s', task and reminder", title)
            return "(not live)"

        doc = actions.gscript(self.cfg, "create_doc", title=title, body=out["letter"])
        doc_url = doc["url"]

        tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
        hh, mm = (self.cfg.get("career_reminder_time") or "15:00").split(":")
        start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, int(hh), int(mm), tzinfo=TZ)
        job_url = final_url or rec.get("url", "")
        desc = (
            f"Review and submit your application.\n\n"
            f"Cover letter draft: {doc_url}\n"
            f"Job ad: {job_url}\n"
            f"Resume: {RESUME_PDF}\n\n"
            f"Wins used: {out.get('wins_used','')}\n"
            f"Check before sending: {out.get('notes_for_brad','')}"
        )
        actions.add_todoist_task(
            self.cfg, f"Apply: {rec['title']} - {rec['company']}", desc, tomorrow.isoformat(), False, False,
            project_id=self.cfg.get("todoist_career_project_id"),
        )
        try:
            actions.gscript(self.cfg, "create_event", title=f"Submit application: {rec['title']} - {rec['company']}",
                            start=start.isoformat(), minutes=30, description=desc)
        except Exception as exc:
            log.warning("Calendar reminder failed: %s", exc)
        actions.notify(
            self.cfg["notify_service"], "📝 Cover letter ready",
            f"{rec['title']} - {rec['company']}. Draft in Drive, to-do added, reminder set for tomorrow.",
            doc_url, False, channel="Job matches", style="letter",
        )
        return doc_url
