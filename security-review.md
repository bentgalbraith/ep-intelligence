# Security review

Reviewed October 5, 2026. The app is hosted on Render. Nothing here has been fixed yet. Work through the items in order.

## Cross-firm client changes

Reading a client checks the firm. Updating, deleting, adding steps, editing steps, and reordering do not. Those routes use only the client or step id, and that id is in the client page URL. Anyone with any firm's tracker code and that id can rename a client, replace the client portal code, or delete the record.

- `PUT` and `DELETE /api/tracker/clients/<client_id>` in `app.py`
- Step routes under `/api/tracker/clients/<client_id>/steps` and `/api/tracker/steps/<step_id>`
- `update_client`, `delete_client`, `add_client_step`, `update_step`, `delete_step`, and `reorder_steps` in `tracker_db.py` do not take a firm id

## Document jobs are not tied to the firm

Document Separator and Prospect Summarizer store the upload in process memory and return a job id. Status, download, and redo look up that id only. Any other signed-in firm with the tool enabled can pull the separated PDFs or the prospect extraction for about 30 minutes. Compare EP Diagram vs. Drafts already rejects a job whose firm does not match.

- `api_doc_separate_status`, `api_doc_separate_download`, `api_doc_separate_download_single`, and `api_doc_separate_redo` in `app.py`
- `api_prospect_summarize_status` in `app.py`
- The separator job also keeps the original PDF in `_jobs` for a redo
- On Render this store is per instance and disappears on restart

## Access codes stored in plaintext

`firms.access_code_plain`, `firms.tracker_access_code_plain`, and `clients.access_code_plain` sit next to the password hashes. A successful firm login also writes the working access code into `login_attempts`, and the admin login log displays it. A database backup is enough to sign in as every firm and every client.

- Columns defined and written in `tracker_db.py` (`create_firm`, `update_firm`, `create_client`, `log_login_attempt`)
- Shown in `templates/admin_login_log.html` and as placeholders in `templates/admin_firm_edit.html`

## Client IP on Render

Nothing trusts `X-Forwarded-For`, so `request.remote_addr` is Render's proxy. Login, admin, and usage-dashboard rate limits (10 per minute) are one shared bucket, and the login log records the proxy address. One caller can use up that bucket and block everyone else from signing in.

The client-portal lockout (20 failures, then 5 minutes) is in memory in `tracker_db.lookup_client`, so a restart or a second instance clears it.

## Quote tooltips run HTML

Drafting Notes, Prospect Summarizer, and Document Differences put a quote into a tooltip with `innerHTML` after the browser has decoded the escaped attribute. A transcript or PDF that contains markup, and that the model copies into a quote, runs in the signed-in user's browser. The content security policy allows inline scripts (`set_security_headers` in `app.py`).

- `tip.innerHTML = icon.dataset.tip` in `templates/drafting_notes.html`, `templates/prospect_summarizer.html`, and `templates/doc_differences.html`

## Staff login is a shared code plus an employee id

The employee id is looked up, not checked as a password (`login_employee` in `app.py`, `get_employee_by_code` in `tracker_db.py`). Anyone who knows the firm code and an id gets that person's session. Firms that do not require an employee id have one shared password for the whole office.

## Uploads go to OpenAI and can exhaust the instance

Transcripts, PDFs, and drafts go to the OpenAI API with no redaction. A signed-in user can also post up to 100MB at a time (`MAX_CONTENT_LENGTH` in `app.py`). Those bytes stay on the worker. Enough of them will run a Render instance out of memory.

## Smaller items

- `/waitlist` and `/onboarding` put the submitted name and email into an HTML email without escaping.
- `/onboarding?session_id=...` shows a payment success page without asking Stripe whether that session was paid. The checkout error response also returns the Stripe exception text (`create_checkout_session` in `app.py`).
