# Security review

Reviewed October 5, 2026. Re-checked and extended October 10, 2026. The app is hosted on Render. Open items come first, in the order to work through them. Fixed items are listed at the end.

## Admin is one password

There is no second factor and no per-account lockout beyond the shared rate limit. Admin can see every firm's plaintext codes, so this one password unlocks every firm and every client.

## Access codes stored in plaintext

`firms.access_code_plain`, `firms.tracker_access_code_plain`, `firms.usage_dashboard_password_plain`, and `clients.access_code_plain` sit next to the password hashes. A successful firm login writes the access code into `login_attempts`, and the admin login log displays it. Failed rows written before October 10, 2026 still hold the typed code (often a near-miss of a real one); clear them with `UPDATE login_attempts SET access_code_used = '' WHERE NOT success;`. `GET /api/tracker/clients/<client_id>` returns the client's plaintext portal code to the browser. A database backup is enough to sign in as every firm and every client.

- Columns defined and written in `tracker_db.py` (`create_firm`, `update_firm`, `create_client`, `update_client`, `get_client`, `log_login_attempt`)
- Shown in `templates/admin_login_log.html` and as placeholders in `templates/admin_firm_edit.html`

## Client IP on Render

Nothing trusts `X-Forwarded-For`, so `request.remote_addr` is Render's proxy. Login, admin, and usage-dashboard rate limits (10 per minute) are one shared bucket, and the login log records the proxy address. One caller can use up that bucket and block everyone else from signing in.

The limiter has no `storage_uri`, so counts are kept in each worker's memory. The real limit is 10 per minute per worker, and it resets on every deploy.

The client-portal lockout (20 failures, then 5 minutes) is in memory in `tracker_db.lookup_client`, so a restart or a second instance clears it.

## Description tooltips run HTML

Field-description tooltips in Drafting Notes and Prospect Summarizer, and the specimen-description tooltip in Document Differences, use `innerHTML`. That text comes from the admin pages, not from uploads. Switch them to `textContent` once you confirm no firm config uses HTML in a description.

## Staff login is a shared code plus an employee id

The employee id is looked up, not checked as a password (`login_employee` in `app.py`, `get_employee_by_code` in `tracker_db.py`). Anyone who knows the firm code and an id gets that person's session. Firms that do not require an employee id have one shared password for the whole office.

## Uploads go to OpenAI and can exhaust the instance

Transcripts, PDFs, and drafts go to the OpenAI API with no redaction. A signed-in user can also post up to 100MB at a time (`MAX_CONTENT_LENGTH` in `app.py`). Those bytes stay on the worker. Enough of them will run a Render instance out of memory. The Document Separator has no page cap, so one very large PDF also runs up the Document AI bill.

The separator job keeps the original PDF in `_jobs` for a redo, and the store is per instance and disappears on restart.

## Smaller items

- Usage-dashboard sessions are not tied to the dashboard password, so changing it does not sign anyone out.
- Alert emails still include the error text and traceback. These no longer contain model output, but an unexpected exception message could still carry client text.

## Fixed October 10, 2026

- **Render start command.** Now `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 180`, with `FLASK_DEBUG` and `FLASK_ENV` unset. Still one worker: do not add workers while jobs live in process memory.
- **Debug server safeguard.** `app.run` enables debug only when `FLASK_DEBUG=1`, and `run.command` sets that for local use.
- **Document jobs tied to the firm.** Every in-memory job and download records the firm when it is created, and routes refuse any other firm the same way they refuse an expired job (`_same_firm` in `app.py`). Covers the separator's status, ZIP download, single-file download, and redo routes, the QPRT routes, Prospect Summarizer status, and the Document Differences export. Compare EP Diagram vs. Drafts and Summarize Documents for Clients already checked.
- **QPRT runs in the background.** The QPRT model call runs as a background job instead of inside the request, so it cannot hit the 30-second timeout.
- **Tracker routes check the firm.** Every tracker client and step route checks the firm in SQL and returns 404 for another firm's client or step. `update_client`, `delete_client`, `add_client_step`, `update_step`, `delete_step`, and `reorder_steps` in `tracker_db.py` take the firm id.
- **Code changes sign people out.** Sessions carry a keyed fingerprint of the firm and tracker code hashes (`_session_codes_current` in `app.py`, cached 30 seconds, cleared on admin save). A new firm code signs everyone out; a new tracker code drops tracker access. Leaving the code fields blank on the admin edit page keeps everyone signed in, but retyping the same code counts as a change (it is rehashed). Sessions from before the change were stamped with the current codes on their next request, not signed out.
- **Admin login hardened.** The password is compared in constant time, and admin sessions expire after 12 hours (`_admin_session_valid`).
- **Failed logins store no code.** `login_attempts` keeps an empty `access_code_used` for failed firm logins.
- **No model output in alerts.** `completion_details` in `ai_logger.py` reports response length only, and the JSON parse error in `doc_separator.py` no longer quotes the response.
- **Stripe checks.** `/onboarding?session_id=...` shows success only when Stripe reports the checkout session complete, and checkout errors return a generic message.
- **Deleted employees are signed out.** At firms that require employee login, `_employee_session_valid` checks that the employee still exists (cached 30 seconds, cleared on delete from admin).
- **Quote tooltips are plain text.** Drafting Notes and Prospect Summarizer quote tooltips render with `textContent` (quote icons carry `data-tip-plain`).
- **Alert emails escape input.** `/waitlist`, `/onboarding`, and the 500 alert email (`_send_error_alert`) escape submitted text, the URL, the user agent, and the traceback.
- **Feedback text kept out of logs.** `redo_with_feedback` in `doc_separator.py` logs only the feedback length.

## Checked and fine

- Every `/admin` route has `admin_required`.
- `update_step` only accepts whitelisted column names.
- No open redirects, and no templates use `|safe`.
- Other `innerHTML` renders (tracker pages, client portal, separator, summaries, comparisons) escape model and user text.
- Every email the app sends escapes user and model text.
