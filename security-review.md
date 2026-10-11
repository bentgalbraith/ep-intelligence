# Security review

Reviewed October 5, 2026. Re-checked and extended October 10, 2026. The app is hosted on Render. Unless a section says otherwise, nothing here has been fixed yet. Work through the items in order.

## Render start command (checked October 10, 2026)

Not a live risk. The start command is `gunicorn app:app --bind 0.0.0.0:$PORT`, and `FLASK_DEBUG` and `FLASK_ENV` are not set. `app.run` now enables debug only when `FLASK_DEBUG=1`, and `run.command` sets that for local use.

The same check showed gunicorn running with its defaults: one sync worker and a 30-second request timeout. A request over 30 seconds kills the worker and every in-memory job with it. Add `--timeout 180` to the start command. Do not add workers while jobs live in process memory.

## Cross-firm client changes

Reading a client checks the firm. Updating, deleting, adding steps, editing steps, and reordering do not. Those routes use only the client or step id, and that id is in the client page URL. Anyone with any firm's tracker code and that id can rename a client, replace the client portal code, or delete the record. Ids are random UUIDs, so an attacker has to obtain one first (shared screen, browser history, logs).

- `PUT` and `DELETE /api/tracker/clients/<client_id>` in `app.py`
- Step routes under `/api/tracker/clients/<client_id>/steps` and `/api/tracker/steps/<step_id>`
- `update_client`, `delete_client`, `add_client_step`, `update_step`, `delete_step`, and `reorder_steps` in `tracker_db.py` do not take a firm id

## Document jobs are not tied to the firm (fixed October 10, 2026)

Every in-memory job and download now records the firm when it is created, and the routes refuse any other firm the same way they refuse an expired job (`_same_firm` in `app.py`). This covers the separator's status, ZIP download, single-file download, and redo routes, Prospect Summarizer status, and the Document Differences export. Compare EP Diagram vs. Drafts, Summarize Documents for Clients, and the QPRT routes already checked.

Still true: the separator job keeps the original PDF in `_jobs` for a redo, and the store is per instance and disappears on restart.

## Sessions cannot be revoked

Firm sessions last 10 days (`PERMANENT_SESSION_LIFETIME` in `app.py`) and live only in the signed cookie. Deleting an employee does not sign them out: `_employee_session_valid` only checks that an `employee_id` is in the session, not that the employee still exists. Changing a firm's access code or tracker code does not sign out anyone who used the old one. A departing staff member keeps access for up to 10 days.

## Admin is one password

`/admin/login` compares the form value to `ADMIN_PASSWORD` with `==`. There is no second factor, no per-account lockout beyond the shared rate limit, and the admin session lasts 10 days like a firm session. Admin can see every firm's plaintext codes, so this one password unlocks every firm and every client.

## Access codes stored in plaintext

`firms.access_code_plain`, `firms.tracker_access_code_plain`, `firms.usage_dashboard_password_plain`, and `clients.access_code_plain` sit next to the password hashes. A firm login writes the typed access code into `login_attempts` on success and on failure, so failed rows often hold a near-miss of a real code, and the admin login log displays them. `GET /api/tracker/clients/<client_id>` returns the client's plaintext portal code to the browser. A database backup is enough to sign in as every firm and every client.

- Columns defined and written in `tracker_db.py` (`create_firm`, `update_firm`, `create_client`, `update_client`, `get_client`, `log_login_attempt`)
- Shown in `templates/admin_login_log.html` and as placeholders in `templates/admin_firm_edit.html`

## Client IP on Render

Nothing trusts `X-Forwarded-For`, so `request.remote_addr` is Render's proxy. Login, admin, and usage-dashboard rate limits (10 per minute) are one shared bucket, and the login log records the proxy address. One caller can use up that bucket and block everyone else from signing in.

The limiter has no `storage_uri`, so counts are kept in each worker's memory. The real limit is 10 per minute per worker, and it resets on every deploy.

The client-portal lockout (20 failures, then 5 minutes) is in memory in `tracker_db.lookup_client`, so a restart or a second instance clears it.

## Quote tooltips run HTML (fixed October 10, 2026)

Drafting Notes and Prospect Summarizer quote tooltips now render with `textContent` (quote icons carry `data-tip-plain`). Quotes are verbatim source text and never contained intended HTML, so nothing changes for users.

Field-description tooltips in those two tools and the specimen-description tooltip in Document Differences still use `innerHTML`. That text comes from the admin pages, not from uploads. Switch them to `textContent` once you confirm no firm config uses HTML in a description.

## Staff login is a shared code plus an employee id

The employee id is looked up, not checked as a password (`login_employee` in `app.py`, `get_employee_by_code` in `tracker_db.py`). Anyone who knows the firm code and an id gets that person's session. Firms that do not require an employee id have one shared password for the whole office.

## Uploads go to OpenAI and can exhaust the instance

Transcripts, PDFs, and drafts go to the OpenAI API with no redaction. A signed-in user can also post up to 100MB at a time (`MAX_CONTENT_LENGTH` in `app.py`). Those bytes stay on the worker. Enough of them will run a Render instance out of memory. The Document Separator has no page cap, so one very large PDF also runs up the Document AI bill.

## Client data in alert emails and logs

Tool-error emails (`_notify_tool_error` in `app.py`) include the error text and up to 500 characters of the model's response, which can hold client names, assets, and family details. They are sent through Resend to a regular inbox. The 500 email includes the full traceback and request URL. `redo_with_feedback` in `doc_separator.py` writes the user's feedback text to Render's logs.

## Smaller items

- `/waitlist` and `/onboarding` put the submitted name and email into an HTML email without escaping. The 500 alert email (`_send_error_alert`) does the same with the URL and user agent.
- `/onboarding?session_id=...` shows a payment success page without asking Stripe whether that session was paid. The checkout error response also returns the Stripe exception text (`create_checkout_session` in `app.py`).

## Checked and fine

- Every `/admin` route has `admin_required`.
- `update_step` only accepts whitelisted column names.
- No open redirects, and no templates use `|safe`.
- Other `innerHTML` renders (tracker pages, client portal, separator, summaries, comparisons) escape model and user text.
