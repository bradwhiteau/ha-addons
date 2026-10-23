# Changelog

## 0.4.2
- Order and parcel emails are archived once they're on the Deliveries board (`archive_deliveries`, on by default). Delivery problems stay in the inbox.

## 0.4.1
- New `email_triage_command` option `catch_up` (with `since: YYYY-MM-DD`): re-runs inbox mail that arrived during dry run as it would have gone live. No phone alerts, and anything already handled for real (job alerts, timesheet, deliveries, subscriptions) is just labelled.

## 0.4.0
- New Subscriptions list: emails from subscription services keep Todoist › Subscriptions up to date. Receipts and renewal notices are added as comments on the matching entry; price changes update the amount and buzz your phone; cancellations move the entry to Cancelled; new sign-ups are added under their billing frequency.
- Phone alert if a cancelled subscription charges you again, or a charge arrives for something not on the list (both land in Needs review).
- These emails get the `AI/Subscription` label.

## 0.3.3
- If the Apps Script can't move a cover letter into the Career Counsellor folder, the to-do and reminder are still created and the reason is logged.

## 0.3.2
- New `email_triage_command` option `retry_failed`: redoes cover letters that failed after you tapped Interested.

## 0.3.1
- Phone notifications are styled by type: their own icon and colour, and grouped so related alerts stack (urgent email red, job matches blue, timesheet tick green, bank-detail warnings orange).

## 0.3.0
- Alerts now go to the Fold (`mobile_app_ecto_8`); they were going to the tablet.
- Every phone notification sent is now logged.
- Urgency: password resets, sign-in codes and passkey/2FA changes are always urgent; appointment reminders more than a day away aren't; Google "granted access" alerts for your own scripts (`known_google_apps`) aren't.
- Unsubscribed mail is archived, plus stragglers from the same sender for 14 days.
- New Deliveries board: order and parcel emails create and move cards in Todoist › Deliveries.
- New `email_triage_command` event (`reask_jobs`) re-sends unanswered job prompts.
- A model that rejects forced tool use is remembered across restarts, so no wasted call each time.

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
