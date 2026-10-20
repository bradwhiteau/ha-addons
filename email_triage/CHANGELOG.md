# Changelog

## 0.2.0
- Career Counsellor: SEEK job alerts are scored against your Job Search Criteria and Career Dossier. Good fits arrive on your phone with Interested / Not for me buttons. Interested drafts a cover letter in Drive (04 Career / Career Counsellor), adds a to-do to General To Do and a calendar reminder for the next day. Every role is logged on the "Job matches" tab.
- Oncore "Timesheet Received" emails send a ✅ phone tick and are logged.
- New keep list (`never_unsubscribe`, default seek.com.au): senders on it are never unsubscribed.
- `assistant_live`: the Career Counsellor and timesheet tick run for real even while `dry_run` is on, since they only notify you and create your own docs.
- Needs the v2 Apps Script helper.

## 0.1.3
- Fix: work with models that don't allow forced tool use (Sonnet 5.5), which broke bill extraction and drafts.
- Emails that can't be triaged after 3 attempts are now written to the Triage log.

## 0.1.2
- Fix: Home Assistant events and phone alerts now get the Supervisor token.
- Trim stray spaces/newlines from pasted keys; clearer message when Gmail rejects the login.

## 0.1.1
- Sheet logger settings are optional and can be left blank.

## 0.1.0
- First version: spam, one-click unsubscribe, urgent alerts, bills to Todoist with bank-detail change check, draft replies, Google Sheet logging, dry-run mode.
