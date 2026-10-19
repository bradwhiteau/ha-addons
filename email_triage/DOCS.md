# Email Triage

Reads each new email in your Gmail inbox the moment it arrives and handles it with Claude:

| Email type | What happens |
|---|---|
| Spam / scams | Moved to Spam and logged. Nothing in it is ever clicked. |
| Marketing from real companies | Unsubscribed using Gmail's one-click standard, only when Gmail verified the sender. Logged with a link back to the email. Otherwise logged as "check manually". |
| Urgent | Push notification to your phone with the subject and why it matters. |
| Bills | Todoist task in **Bills**: amount, due date, payment link, BPAY or BSB/account, link to the email. If a regular biller's bank details change, the task and a phone alert are flagged ⚠️. |
| Needs a reply | Draft reply saved in Gmail Drafts (label `AI draft`), original labelled `AI/Draft ready`. Your Proactive Check-in lists them. **Never sent.** |

Every email it handles gets the `AI/Triaged` label, and every decision is written to the **Triage log** tab of the *Email Unsubscribe Log* sheet.

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
| `notify_service` | Which phone gets alerts, e.g. `mobile_app_ecto_9`. |
| `sheet_webhook_url` / `sheet_webhook_secret` | From the sheet logger Apps Script. |
| `auto_unsubscribe` | Turn off to log marketing without unsubscribing. |
| `create_drafts` | Turn off to stop draft replies. |

## Costs

Roughly US$1–3 a month for a typical personal inbox: every email goes through the fast model; only bills and emails needing a reply use the bigger one.
