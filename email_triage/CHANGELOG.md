# Changelog

## 0.7.0
- Job application updates (interview, offer, rejection, "not progressing") always alert your phone (💼 Application update) and stay in the inbox; they're never treated as FYI.
- Fix: long SEEK alerts (e.g. 9 jobs) could come back as "no jobs found" and be archived. The job list now has room, and an alert that can't be read stays in the inbox.
- Only SEEK's job-alert address goes to the Career Counsellor.
- New `email_triage_command` option `rerun_jobs` (with `since: YYYY-MM-DD`) re-runs inbox job alerts.

## 0.6.1
- Household service bills (internet, mobile, electricity, gas, water, council rates) now go to both Bills (a Todoist payment task, as before) and the Subscriptions list, so their cards stay current. Insurance, rent and loan bills stay Bills-only.
- Usage-based bills (electricity, gas, water) update the card's amount quietly each time, with no price-change alert. Fixed-price ones (internet, mobile, rates) still alert when the price changes.
- A household bill not yet on the list goes to Needs review without a phone alert (the Bills flow already covers it).
- Bi-monthly and Quarterly sections are created when a bill like water or rates needs one.

## 0.6.0
- Subscriptions cards now follow the standard format: title "Service — $amount CUR", description lines Amount AUD, Amount USD, Frequency, Payment method, Work-use % (TBC on new cards), and an optional Notes line. Existing cards are tidied into that order whenever an email touches them.
- Annual subscriptions get a recurring yearly due date on the next renewal (instead of a "Next renewal" line), so the Proactive Check-in can flag them. Monthly ones get no due date.
- Price changes update the Amount AUD/USD line as well as the title; the old price goes into Notes. Trial end dates go into Notes too, and only become the due date if the card has no renewal date.
- Cancelled cards lose their due date. Weekly and quarterly subscriptions now land in Needs review instead of creating their own sections.

## 0.5.1
- New `archive_fyi` (off / trial / on, default trial): purely informational emails that need nothing from you are archived. In trial they're only marked "[trial] would archive: FYI only" in the Triage log, for review.

## 0.5.0
- Inbox zero: emails the agent fully handles are archived (never deleted): job alerts once the Career Counsellor has logged them, Oncore timesheet confirmations, bills once the Todoist task exists (or they're autopay), subscription emails once the Subscriptions list is updated, and deliveries.
- Kept in the inbox: anything urgent, needing a reply, a bill with changed bank details, a subscription that triggered an alert, a delivery problem, or anything that failed.
- New `archive_handled` setting (on by default) turns this off.

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
