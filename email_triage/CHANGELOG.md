# Changelog

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
