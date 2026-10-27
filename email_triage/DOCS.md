# Email Triage

Reads each new email in your Gmail inbox the moment it arrives and handles it with Claude:

| Email type | What happens |
|---|---|
| Spam / scams | Moved to Spam and logged. Nothing in it is ever clicked. |
| Marketing from real companies | Unsubscribed using Gmail's one-click standard, only when Gmail verified the sender, then archived (label `AI/Unsubscribed`, still in All Mail). Anything else from that sender in the next 14 days is archived too, while the unsubscribe takes effect. Otherwise logged as "check manually". |
| Urgent | Push notification to your phone with the subject and why it matters. Password resets, sign-in codes and passkey/2FA changes always count. Reminders for appointments more than a day away don't. Google "app granted access" alerts for apps on your `known_google_apps` list don't. |
| Orders and parcels | Card on the Todoist **Deliveries** board, moved between columns as emails arrive (Ordered → On it's way → Arrived, or Follow-ups / Returns, Cancelled / Returned). Each email is added as a comment on the card, then archived (delivery problems stay in the inbox). |
| Subscriptions | Todoist **Subscriptions** list kept up to date (see below). |
| Bills | Todoist task in **Bills**: amount, due date, payment link, BPAY or BSB/account, link to the email. If a regular biller's bank details change, the task and a phone alert are flagged ⚠️. |
| SEEK job alerts | Sent to the **Career Counsellor** (below). Never unsubscribed. |
| Oncore "Timesheet Received" | ✅ phone tick and a log entry. |
| Needs a reply | Draft reply saved in Gmail Drafts (label `AI draft`), original labelled `AI/Draft ready`. Your Proactive Check-in lists them. **Never sent.** |

Every email it handles gets the `AI/Triaged` label, and every decision is written to the **Triage log** tab of the *Email Unsubscribe Log* sheet.

## Inbox zero

Anything the agent fully deals with is archived: it stays in All Mail and keeps its `AI/...` label, so you can always find it (e.g. search `label:ai-bill`). Archived: job alerts, timesheet ticks, bills with a Todoist task (or autopay), subscription updates and deliveries. Left in your inbox: anything urgent, needing a reply, with changed bank details, that set off a subscription alert, a delivery problem, or anything that failed. Turn it off with `archive_handled`.

Purely informational emails (completed DocuSigns, privacy-policy updates, application confirmations) are covered by `archive_fyi`: `trial` only marks them in the Triage log as "would archive", `on` archives them, `off` leaves them alone. When the agent is unsure, it leaves the email in the inbox.

## Career Counsellor

Each SEEK alert is split into roles and scored against two docs in Drive: **Job Search Criteria** (04 Career / Career Counsellor, what you want and don't) and your **Master Career Profile & Cover Letter Dossier** (your background and wins). Good fits (up to 5 per alert) come to your phone with **Interested** / **Not for me** buttons. Tap **Interested** and it:

1. reads the full ad on SEEK (or writes from the alert summary if it can't),
2. drafts a cover letter to your rules and saves it as a Google Doc in 04 Career / Career Counsellor,
3. adds "Apply: role - company" to Todoist General To Do (due tomorrow) with links to the draft, the ad and your resume,
4. adds a calendar reminder for tomorrow at `career_reminder_time`.

Every role, including the ones it skipped and why, goes on the **Job matches** tab. It never applies for anything.

## Subscriptions

Emails from recurring paid services (streaming, software, apps, storage, memberships, Patreon and other recurring donations) update Todoist › **Subscriptions**. Household service bills (internet, mobile, electricity, gas, water, council rates) do both: they get a Bills payment task and update their Subscriptions card. Usage-based ones (electricity, gas, water) just record the latest amount, with no price-change alert. Insurance, rent and loan bills stay with Bills only. Each email is matched to an entry by service name and added to it as a comment, so every entry keeps its own history.

Every card uses the same format: title "Service — $amount CUR"; description lines **Amount AUD**, **Amount USD**, **Frequency**, **Payment method**, **Work-use %**, plus an optional **Notes** line. Annual subscriptions carry a recurring yearly due date on the next renewal; monthly ones have no due date.

| Email says | What happens |
|---|---|
| Receipt / payment taken | Comment. If the amount differs from the one in the title (same currency), the title and Amount line are updated, the old amount noted in Notes, and your phone is alerted. For annual subscriptions, the due date moves to the next renewal if the email gives one. |
| Price change | As above. |
| Renewing soon | Comment; for annual subscriptions, the recurring due date is set to the renewal date. |
| Trial ending | Comment and "Trial ends" in Notes; due that day unless the card already has a renewal date. |
| Payment failed | Comment (and an urgent alert, as before). |
| Cancelled | Moved to **Cancelled**, due date removed. |
| New sign-up | Added under Monthly / Bi-monthly / Quarterly / Annual (section created if needed), or moved back there if it was cancelled. Anything else goes to Needs review. |
| Charged after cancelling | Moved to **Needs review**, phone alert. |
| Charge for something not on the list | Added to **Needs review**, phone alert (no alert for household bills, which Bills already covers). |

If it isn't sure which entry an email belongs to, it adds a new one to Needs review rather than guessing. Entries are only ever added, commented on, renamed or moved; it never deletes or completes them. Runs with `assistant_live`, like Deliveries.

## What it will never do

Send email, delete email, click links inside emails, pay anything, or act on instructions written inside an email.

## How it's triggered

Home Assistant's IMAP integration fires an `imap_content` event when mail arrives. The add-on uses that as a wake-up, then reads every inbox message it hasn't handled yet, so a burst of emails isn't missed. It also sweeps every `sweep_minutes` in case an event is dropped. It only handles mail that arrives after its first start; it does not go back through old mail.

## Settings

| Setting | What it is |
|---|---|
| `dry_run` | **Start with this on.** Claude still reads and classifies each email and the Google Sheet log is filled in (marked `[dry run]`), but nothing in Gmail, Todoist or your phone changes. Turn it off once the log looks right. |
| `gmail_address` | Your Gmail address. |
| `gmail_app_password` | The Gmail app password (spaces are fine). |
| `anthropic_api_key` | Your Anthropic API key. |
| `triage_model` | Fast model that sorts every email. |
| `reasoning_model` | Model used only for bills and draft replies. |
| `todoist_api_token` | Todoist › Settings › Integrations › Developer (web only). |
| `todoist_bills_project_id` | The Bills project (already filled in). |
| `notify_service` | Which phone gets alerts: `mobile_app_ecto_8` (the Fold). |
| `sheet_webhook_url` / `sheet_webhook_secret` | From the sheet logger Apps Script. |
| `auto_unsubscribe` | Turn off to log marketing without unsubscribing. |
| `create_drafts` | Turn off to stop draft replies. |
| `never_unsubscribe` | Sender domains that are never unsubscribed (keep list). |
| `assistant_live` | Career Counsellor and timesheet tick run for real even in dry run. Turn off to test them quietly too. |
| `career_enabled` | Turn the Career Counsellor on or off. |
| `todoist_career_project_id` | Where application to-dos go (General To Do). |
| `career_reminder_time` | Time of the next-day application reminder (24h, Perth). |
| `known_google_apps` | Names of your own Apps Script projects. Google "granted access" alerts for these don't buzz your phone. Add a name whenever you authorise a new script. |
| `archive_after_unsubscribe_days` | Archive unsubscribed mail, and stragglers from that sender for this many days. 0 turns archiving off. |
| `deliveries_enabled` / `todoist_deliveries_project_id` | The Deliveries board. Runs with `assistant_live`, like the Career Counsellor. |
| `subscriptions_enabled` / `todoist_subscriptions_project_id` | The Subscriptions list. Runs with `assistant_live`. |

To re-send Career Counsellor prompts you haven't answered yet, fire the Home Assistant event `email_triage_command` with data `command: reask_jobs` (Developer tools › Events). Use `command: retry_failed` to redo cover letters that failed after you tapped Interested.

## Costs

Roughly US$1–3 a month for a typical personal inbox: every email goes through the fast model; only bills and emails needing a reply use the bigger one.
