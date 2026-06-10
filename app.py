import collections
import io
import json
import os
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, Response, has_request_context, jsonify, redirect, render_template, request, session, url_for
from flask_cors import cross_origin
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from openai import OpenAI

from ai_logger import log_ai_call, extract_xai_usage
from doc_separator import separate_documents, redo_with_feedback, _extract_json
from ep_export import build_export_csv, build_questionnaire_docx
from prospect_summarizer import extract_prospect_documents, build_summary_docx, PROSPECT_SCHEMA
from quote_verify import verify_quotes
import resend
import tracker_db

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

limiter = Limiter(get_remote_address, app=app)

XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-1-fast-reasoning")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

xai_client = OpenAI(
    api_key=os.environ["XAI_API_KEY"],
    base_url=os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
    timeout=int(os.environ.get("XAI_TIMEOUT", "120")),
)

if tracker_db.DATABASE_URL:
    tracker_db.init_db()
    tracker_db.seed_firm_if_empty()


# ---------------------------------------------------------------------------
# Firm config helpers
# ---------------------------------------------------------------------------

_firm_config_cache: dict = {}
_firm_config_lock = threading.Lock()
_CACHE_TTL = 300  # 5 minutes


def _get_firm_config(firm_id):
    """Load firm config from DB with in-memory caching."""
    now = time.time()
    with _firm_config_lock:
        cached = _firm_config_cache.get(str(firm_id))
        if cached and now - cached["ts"] < _CACHE_TTL:
            return cached["config"]

    config = tracker_db.get_firm_config(str(firm_id))
    with _firm_config_lock:
        _firm_config_cache[str(firm_id)] = {"config": config, "ts": now}
    return config


def _get_ep_schema(firm_config):
    ep = firm_config.get("ep_schema")
    if not ep or not ep.get("sections"):
        raise ValueError("Firm config is missing 'ep_schema'. Every firm must have a complete configuration.")
    return ep



def _get_prospect_schema(firm_config):
    ps = firm_config.get("prospect_schema")
    if not ps or not ps.get("sections"):
        raise ValueError("Firm config is missing 'prospect_schema'. Every firm must have a complete configuration.")
    return ps


def _build_ep_extraction_prompt(firm_config):
    firm_context = firm_config.get("firm_context")
    if not firm_context:
        raise ValueError("Firm config is missing 'firm_context'. Every firm must have a complete configuration.")

    prompt = """\
You are a legal assistant specializing in estate planning. You will receive a \
transcript from an introductory client meeting and optional additional notes.

Your job is to extract structured data from the transcript according to a \
schema I will provide. For EVERY field you can answer, return:
- "value": a concise, form-ready answer. For short-answer fields (names, \
roles), the exact name or phrase from the transcript is fine. For longer \
narrative fields, synthesize the relevant information into a clean summary \
rather than pasting a block of transcript verbatim.
- "quote": the exact phrase(s) from the transcript that support your answer. \
Copy the words verbatim. If multiple parts of the transcript are relevant, \
join them with " ... ". Every non-null value MUST have an accompanying quote.

If a field cannot be answered from the transcript, set both value and quote \
to null. Do not guess or fabricate.

Each field in the schema has a "type" property. Handle each type as follows:
- "text": set "value" to a concise synthesized answer (not raw transcript). \
Capitalize the first letter (sentence-style).
- "yes_no": set "value" to exactly "Yes" or "No". Never "yes", "no", "true", \
or "false".
- "choice": set "value" to exactly one of the strings listed in the field's \
"options" array. Use the exact spelling and capitalization from the options.

Return ONLY valid JSON with this structure (no markdown, no commentary):
{
  "sections": {
    "<section_id>": {
      "fields": {
        "<field_id>": {"value": "...", "quote": "..."},
        ...
      }
    }
  }
}
"""
    if firm_context:
        prompt += f"\nFirm context: {firm_context}\n"

    prompt += "\nHere is the schema:\n"
    return prompt


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

@app.after_request
def set_security_headers(response):
    if not request.host.startswith("127.0.0.1") and not request.host.startswith("localhost"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://js.stripe.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' https://api.stripe.com; "
        "img-src 'self' data: https://*.stripe.com; "
        "frame-src https://js.stripe.com https://hooks.stripe.com; "
        "frame-ancestors 'none'"
    )
    return response


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def tracker_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        if not session.get("tracker_authenticated"):
            return jsonify({"error": "Tracker access required"}), 403
        return f(*args, **kwargs)
    return decorated


def _is_tool_enabled(tool_key):
    """Check whether a tool is enabled for the current firm."""
    firm_id = session.get("firm_id")
    if not firm_id:
        return True
    config = _get_firm_config(firm_id) or {}
    return config.get("tools_enabled", {}).get(tool_key, True)


def tool_enabled(tool_key):
    """Decorator that blocks access when a tool is disabled for the firm."""
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not _is_tool_enabled(tool_key):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "This tool is not enabled for your firm."}), 403
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return wrapper


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/waitlist", methods=["GET", "POST"])
@limiter.limit("5/minute")
def waitlist():
    if request.method == "GET":
        return render_template("waitlist.html")

    firm_name = request.form.get("firm_name", "").strip()
    contact_name = request.form.get("contact_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    firm_size = request.form.get("firm_size", "").strip()

    if not all([firm_name, contact_name, email, firm_size]):
        return render_template("waitlist.html", error="Please fill in all required fields.")

    body = (
        f"<h2>New Waitlist Signup</h2>"
        f"<p><strong>Firm Name:</strong> {firm_name}</p>"
        f"<p><strong>Contact Name:</strong> {contact_name}</p>"
        f"<p><strong>Phone:</strong> {phone or '(not provided)'}</p>"
        f"<p><strong>Email:</strong> {email}</p>"
        f"<p><strong>Firm Size:</strong> {firm_size}</p>"
    )

    if RESEND_API_KEY:
        try:
            resend.Emails.send({
                "from": "EP Intelligence <notifications@ep-intelligence.com>",
                "to": ["ben@ep-intelligence.com"],
                "subject": f"Waitlist: {firm_name}",
                "html": body,
            })
        except Exception:
            traceback.print_exc()
    else:
        app.logger.warning("RESEND_API_KEY not set — waitlist email not sent")

    return render_template("waitlist.html", success=True)


@app.route("/onboarding", methods=["GET", "POST"])
@limiter.limit("5/minute")
def onboarding():
    if request.method == "GET":
        return render_template("onboarding.html")

    firm_name = request.form.get("firm_name", "").strip()
    contact_name = request.form.get("contact_name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    firm_size = request.form.get("firm_size", "").strip()

    if not all([firm_name, contact_name, email, firm_size]):
        return render_template("onboarding.html", error="Please fill in all required fields.")

    body = (
        f"<h2>New Firm Onboarding</h2>"
        f"<p><strong>Firm Name:</strong> {firm_name}</p>"
        f"<p><strong>Contact Name:</strong> {contact_name}</p>"
        f"<p><strong>Phone:</strong> {phone or '(not provided)'}</p>"
        f"<p><strong>Email:</strong> {email}</p>"
        f"<p><strong>Firm Size:</strong> {firm_size}</p>"
    )

    if RESEND_API_KEY:
        try:
            resend.Emails.send({
                "from": "EP Intelligence <notifications@ep-intelligence.com>",
                "to": ["ben@ep-intelligence.com"],
                "subject": f"Onboarding: {firm_name}",
                "html": body,
            })
        except Exception:
            traceback.print_exc()
    else:
        app.logger.warning("RESEND_API_KEY not set — onboarding email not sent")

    return render_template("onboarding.html", step="payment")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        code = request.form.get("access_code", "")
        firm = tracker_db.lookup_firm_by_access_code(code)
        if firm:
            session["authenticated"] = True
            session["firm_id"] = str(firm["id"])
            session["firm_name"] = firm["name"]
            session["firm_slug"] = firm["slug"]
            return redirect(url_for("dashboard"))
        error = "Invalid access code"

    return render_template("login.html", error=error)


@app.route("/dashboard")
@login_required
def dashboard():
    firm_config = _get_firm_config(session.get("firm_id"))
    tools_enabled = (firm_config or {}).get("tools_enabled", {})
    return render_template("dashboard.html", firm_name=session.get("firm_name", ""),
                           tools_enabled=tools_enabled)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Drafting Notes
# ---------------------------------------------------------------------------

@app.route("/drafting-notes")
@login_required
@tool_enabled("drafting_notes")
def drafting_notes():
    return render_template("drafting_notes.html", firm_name=session.get("firm_name", ""))


@app.route("/ep-diagram")
@login_required
def ep_diagram_redirect():
    return redirect(url_for("drafting_notes"))


@app.route("/api/ep-extract", methods=["POST"])
@login_required
@tool_enabled("drafting_notes")
def api_ep_extract():
    data = request.get_json()
    transcript = (data.get("transcript") or "").strip()
    notes = (data.get("notes") or "").strip()

    if not transcript:
        return jsonify({"error": "Transcript is required."}), 400

    firm_id = session.get("firm_id")
    firm_config = _get_firm_config(firm_id)
    ep_schema = _get_ep_schema(firm_config)

    schema_text = json.dumps(ep_schema["sections"], indent=2)
    system_content = _build_ep_extraction_prompt(firm_config) + schema_text

    user_content = f"TRANSCRIPT:\n{transcript}"
    if notes:
        user_content += f"\n\nADDITIONAL NOTES:\n{notes}"

    call_start = time.time()
    try:
        response = xai_client.chat.completions.create(
            model=XAI_MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
        )
        log_ai_call(
            provider="xai", model=XAI_MODEL, tool="ep_extract", status="success",
            execution_ms=int((time.time() - call_start) * 1000),
            firm_id=firm_id,
            **extract_xai_usage(response),
        )
        raw = response.choices[0].message.content.strip()

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        extraction = _extract_json(raw)
        verify_quotes(extraction, transcript)
        extraction["schema"] = ep_schema["sections"]
        return jsonify(extraction)
    except json.JSONDecodeError as e:
        app.logger.error("JSON parse error: %s\nRaw response: %s", e, raw[:500])
        _notify_tool_error("Drafting Notes", str(e), firm_id=firm_id)
        return jsonify({"error": "AI returned invalid JSON. Please try again."}), 500
    except Exception as e:
        log_ai_call(
            provider="xai", model=XAI_MODEL, tool="ep_extract", status="error",
            execution_ms=int((time.time() - call_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        app.logger.error("EP extract error: %s", e)
        _notify_tool_error("Drafting Notes", str(e), firm_id=firm_id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ep-export-csv", methods=["POST"])
@login_required
@tool_enabled("drafting_notes")
def api_ep_export_csv():
    data = request.get_json() or {}
    sections = data.get("sections") or {}
    schema = data.get("schema") or []

    try:
        buf = build_export_csv(sections, schema)
        return Response(
            buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=EP_Raw_Data.csv"},
        )
    except Exception as e:
        app.logger.error("CSV export error: %s", e)
        _notify_tool_error("CSV Export", str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/ep-export-docx", methods=["POST"])
@login_required
@tool_enabled("drafting_notes")
def api_ep_export_docx():
    data = request.get_json() or {}
    sections = data.get("sections") or {}
    schema = data.get("schema") or []

    try:
        buf = build_questionnaire_docx(sections, schema)
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": "attachment; filename=EP_Questionnaire.docx"},
        )
    except Exception as e:
        app.logger.error("DOCX export error: %s", e)
        _notify_tool_error("DOCX Export", str(e))
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Document Separator
# ---------------------------------------------------------------------------

_zip_cache: dict = {}
_jobs: dict = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2)

_JOB_TTL = 1800


def _purge_stale(store):
    now = time.time()
    for k in list(store):
        if now - store[k].get("ts", 0) > _JOB_TTL:
            del store[k]


def _run_doc_separate(job_id, pdf_content, firm_id, firm_config):
    try:
        zip_buf, documents, page_texts, total_pages = separate_documents(
            pdf_content, xai_client, firm_id=firm_id, firm_config=firm_config,
        )
        token = uuid.uuid4().hex
        with _jobs_lock:
            _zip_cache[token] = {"data": zip_buf.getvalue(), "ts": time.time()}
            _jobs[job_id].update({
                "status": "complete",
                "documents": documents,
                "download_token": token,
                "page_texts": page_texts,
                "total_pages": total_pages,
                "pdf_content": pdf_content,
                "firm_id": firm_id,
                "firm_config": firm_config,
            })
    except Exception as e:
        app.logger.error("Doc separator error (job %s): %s", job_id, e)
        _notify_tool_error("Document Separator", str(e), firm_id=firm_id,
                           firm_name=_jobs[job_id].get("firm_name"),
                           firm_slug=_jobs[job_id].get("firm_slug"))
        with _jobs_lock:
            _jobs[job_id].update({"status": "error", "error": str(e)})


@app.route("/doc-separator")
@login_required
@tool_enabled("doc_separator")
def doc_separator():
    return render_template("doc_separator.html", firm_name=session.get("firm_name", ""))


@app.route("/api/doc-separate", methods=["POST"])
@login_required
@tool_enabled("doc_separator")
def api_doc_separate():
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file."}), 400

    pdf_content = f.read()
    if not pdf_content:
        return jsonify({"error": "The uploaded file is empty."}), 400

    firm_id = session.get("firm_id")
    firm_config = _get_firm_config(firm_id)

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _purge_stale(_jobs)
        _purge_stale(_zip_cache)
        _jobs[job_id] = {"status": "processing", "ts": time.time(),
                         "firm_name": session.get("firm_name"),
                         "firm_slug": session.get("firm_slug")}

    _executor.submit(_run_doc_separate, job_id, pdf_content, firm_id, firm_config)
    return jsonify({"job_id": job_id})


@app.route("/api/doc-separate/status/<job_id>")
@login_required
@tool_enabled("doc_separator")
def api_doc_separate_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found or expired."}), 404

    if job["status"] == "processing":
        return jsonify({"status": "processing"})

    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]}), 500

    return jsonify({
        "status": "complete",
        "documents": job["documents"],
        "download_token": job["download_token"],
    })


@app.route("/api/doc-separate/download/<token>")
@login_required
@tool_enabled("doc_separator")
def api_doc_separate_download(token):
    with _jobs_lock:
        entry = _zip_cache.get(token)

    if not entry:
        return jsonify({"error": "Download expired or not found."}), 404

    return Response(
        entry["data"],
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=Separated_Documents_{time.strftime('%m-%d-%Y_%H%M%S')}.zip"},
    )


@app.route("/api/doc-separate/download/<token>/<int:doc_index>")
@login_required
@tool_enabled("doc_separator")
def api_doc_separate_download_single(token, doc_index):
    with _jobs_lock:
        entry = _zip_cache.get(token)

    if not entry:
        return jsonify({"error": "Download expired or not found."}), 404

    zip_buf = io.BytesIO(entry["data"])
    with zipfile.ZipFile(zip_buf, "r") as zf:
        names = zf.namelist()
        if doc_index < 0 or doc_index >= len(names):
            return jsonify({"error": "Document not found."}), 404
        filename = names[doc_index]
        pdf_data = zf.read(filename)

    from urllib.parse import quote
    return Response(
        pdf_data,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )


def _run_doc_separate_redo(job_id, pdf_content, page_texts, total_pages,
                           previous_documents, feedback, firm_id, firm_config):
    try:
        zip_buf, documents = redo_with_feedback(
            pdf_content, xai_client, page_texts, total_pages,
            previous_documents, feedback,
            firm_id=firm_id, firm_config=firm_config,
        )
        token = uuid.uuid4().hex
        with _jobs_lock:
            _zip_cache[token] = {"data": zip_buf.getvalue(), "ts": time.time()}
            _jobs[job_id].update({
                "status": "complete",
                "documents": documents,
                "download_token": token,
                "page_texts": page_texts,
                "total_pages": total_pages,
                "pdf_content": pdf_content,
                "firm_id": firm_id,
                "firm_config": firm_config,
            })
    except Exception as e:
        app.logger.error("Doc separator redo error (job %s): %s", job_id, e)
        _notify_tool_error("Document Separator (Redo)", str(e), firm_id=firm_id,
                           firm_name=_jobs[job_id].get("firm_name"),
                           firm_slug=_jobs[job_id].get("firm_slug"))
        with _jobs_lock:
            _jobs[job_id].update({"status": "error", "error": str(e)})


@app.route("/api/doc-separate/redo", methods=["POST"])
@login_required
@tool_enabled("doc_separator")
def api_doc_separate_redo():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request."}), 400

    original_job_id = data.get("job_id", "").strip()
    feedback = data.get("feedback", "").strip()

    if not original_job_id or not feedback:
        return jsonify({"error": "job_id and feedback are required."}), 400

    with _jobs_lock:
        original_job = _jobs.get(original_job_id)

    if not original_job or original_job.get("status") != "complete":
        return jsonify({"error": "Original job not found or not complete."}), 404

    page_texts = original_job.get("page_texts")
    total_pages = original_job.get("total_pages")
    pdf_content = original_job.get("pdf_content")
    previous_documents = original_job.get("documents")
    firm_id = original_job.get("firm_id")
    firm_config = original_job.get("firm_config")
    if not firm_config:
        return jsonify({"error": "Original job data expired. Please re-upload."}), 410

    if not page_texts or not pdf_content:
        return jsonify({"error": "Original job data expired. Please re-upload."}), 410

    new_job_id = uuid.uuid4().hex
    with _jobs_lock:
        _purge_stale(_jobs)
        _purge_stale(_zip_cache)
        _jobs[new_job_id] = {"status": "processing", "ts": time.time(),
                             "firm_name": session.get("firm_name"),
                             "firm_slug": session.get("firm_slug")}

    _executor.submit(
        _run_doc_separate_redo, new_job_id, pdf_content, page_texts,
        total_pages, previous_documents, feedback, firm_id, firm_config,
    )
    return jsonify({"job_id": new_job_id})


# ---------------------------------------------------------------------------
# Prospect Summarizer
# ---------------------------------------------------------------------------

@app.route("/prospect-summarizer")
@login_required
@tool_enabled("prospect_summarizer")
def prospect_summarizer():
    return render_template("prospect_summarizer.html", firm_name=session.get("firm_name", ""))


def _run_prospect_summarize(job_id, pdf_contents, notes, firm_id, firm_config):
    try:
        prospect_schema = _get_prospect_schema(firm_config)
        extraction, ocr_text = extract_prospect_documents(
            pdf_contents, xai_client, notes=notes,
            firm_id=firm_id, firm_config=firm_config,
        )
        verify_quotes(extraction, ocr_text)
        extraction["schema"] = prospect_schema["sections"]
        with _jobs_lock:
            _jobs[job_id].update({"status": "complete", "extraction": extraction})
    except Exception as e:
        app.logger.error("Prospect summarizer error (job %s): %s", job_id, e)
        _notify_tool_error("Prospect Summarizer", str(e), firm_id=firm_id,
                           firm_name=_jobs[job_id].get("firm_name"),
                           firm_slug=_jobs[job_id].get("firm_slug"))
        with _jobs_lock:
            _jobs[job_id].update({"status": "error", "error": str(e)})


@app.route("/api/prospect-summarize", methods=["POST"])
@login_required
@tool_enabled("prospect_summarizer")
def api_prospect_summarize():
    files = request.files.getlist("pdfs")
    if not files or not any(f.filename for f in files):
        return jsonify({"error": "Please upload at least one PDF file."}), 400

    pdf_contents = []
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            return jsonify({"error": f"'{f.filename}' is not a PDF."}), 400
        data = f.read()
        if not data:
            return jsonify({"error": f"'{f.filename}' is empty."}), 400
        pdf_contents.append(data)

    notes = request.form.get("notes", "")
    firm_id = session.get("firm_id")
    firm_config = _get_firm_config(firm_id)

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _purge_stale(_jobs)
        _jobs[job_id] = {"status": "processing", "ts": time.time(),
                         "firm_name": session.get("firm_name"),
                         "firm_slug": session.get("firm_slug")}

    _executor.submit(_run_prospect_summarize, job_id, pdf_contents, notes, firm_id, firm_config)
    return jsonify({"job_id": job_id})


@app.route("/api/prospect-summarize/status/<job_id>")
@login_required
@tool_enabled("prospect_summarizer")
def api_prospect_summarize_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found or expired."}), 404

    if job["status"] == "processing":
        return jsonify({"status": "processing"})

    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]}), 500

    return jsonify({"status": "complete", "extraction": job["extraction"]})


@app.route("/api/prospect-summary-docx", methods=["POST"])
@login_required
@tool_enabled("prospect_summarizer")
def api_prospect_summary_docx():
    data = request.get_json() or {}
    sections = data.get("sections") or {}
    schema = data.get("schema")
    filenames = data.get("filenames") or []

    if not schema:
        firm_id = session.get("firm_id")
        firm_config = _get_firm_config(firm_id)
        schema = _get_prospect_schema(firm_config).get("sections", [])

    if not sections:
        return jsonify({"error": "No data to export."}), 400

    try:
        buf = build_summary_docx(sections, schema, filenames=filenames)
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": "attachment; filename=Prospect_EP_Summary.docx"},
        )
    except Exception as e:
        app.logger.error("Prospect summary DOCX export error: %s", e)
        _notify_tool_error("Prospect Summary DOCX", str(e))
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Tracker admin
# ---------------------------------------------------------------------------

@app.route("/api/tracker-auth", methods=["POST"])
@login_required
@tool_enabled("tracker")
@limiter.limit("10/minute")
def api_tracker_auth():
    from werkzeug.security import check_password_hash

    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    firm_id = session.get("firm_id")
    if not firm_id:
        return jsonify({"error": "Not authenticated"}), 401

    with tracker_db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tracker_access_code_hash FROM firms WHERE id = %s", (firm_id,))
            row = cur.fetchone()
            if row and check_password_hash(row[0], code):
                session["tracker_authenticated"] = True
                return jsonify({"ok": True})

    return jsonify({"error": "Invalid access code"}), 401


@app.route("/client-progress")
@login_required
@tool_enabled("tracker")
def client_progress():
    if not session.get("tracker_authenticated"):
        return redirect(url_for("dashboard"))
    return render_template("client_progress.html", firm_name=session.get("firm_name", ""))


@app.route("/client-progress/<client_id>")
@login_required
@tool_enabled("tracker")
def client_detail(client_id):
    if not session.get("tracker_authenticated"):
        return redirect(url_for("dashboard"))
    return render_template("client_detail.html", client_id=client_id, firm_name=session.get("firm_name", ""))


@app.route("/api/tracker/clients", methods=["GET"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_list_clients():
    firm_id = session.get("firm_id")
    return jsonify(tracker_db.list_clients(firm_id))


@app.route("/api/tracker/clients", methods=["POST"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_create_client():
    data = request.get_json() or {}
    name = (data.get("client_name") or "").strip()
    code = (data.get("client_id_code") or "").strip()
    pw = (data.get("access_code") or "").strip()
    if not name or not code or not pw:
        return jsonify({"error": "client_name, client_id_code, and access_code are required."}), 400
    firm_id = session.get("firm_id")
    try:
        cid = tracker_db.create_client(firm_id, name, code, pw)
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "A client with that ID already exists."}), 409
        raise
    return jsonify({"id": cid}), 201


@app.route("/api/tracker/clients/<client_id>", methods=["GET"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_get_client(client_id):
    firm_id = session.get("firm_id")
    client = tracker_db.get_client(client_id, firm_id=firm_id)
    if not client:
        return jsonify({"error": "Client not found."}), 404
    client["steps"] = tracker_db.get_client_steps(client_id)
    return jsonify(client)


@app.route("/api/tracker/clients/<client_id>", methods=["PUT"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_update_client(client_id):
    data = request.get_json() or {}
    kwargs = {}
    if "client_name" in data:
        kwargs["client_name"] = data["client_name"].strip()
    if "client_id_code" in data:
        kwargs["client_id_code"] = data["client_id_code"].strip()
    if "access_code" in data and data["access_code"].strip():
        kwargs["access_code"] = data["access_code"].strip()
    try:
        tracker_db.update_client(client_id, **kwargs)
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "A client with that ID already exists."}), 409
        raise
    return jsonify({"ok": True})


@app.route("/api/tracker/clients/<client_id>", methods=["DELETE"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_delete_client(client_id):
    tracker_db.delete_client(client_id)
    return jsonify({"ok": True})


@app.route("/api/tracker/clients/<client_id>/steps", methods=["POST"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_add_step(client_id):
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Step name is required."}), 400
    desc = (data.get("description") or "").strip()
    sort_order = data.get("sort_order")
    sid = tracker_db.add_client_step(client_id, name, desc, sort_order)
    return jsonify({"id": sid}), 201


@app.route("/api/tracker/steps/<step_id>", methods=["PUT"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_update_step(step_id):
    data = request.get_json() or {}
    tracker_db.update_step(step_id, **data)
    return jsonify({"ok": True})


@app.route("/api/tracker/steps/<step_id>", methods=["DELETE"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_delete_step(step_id):
    tracker_db.delete_step(step_id)
    return jsonify({"ok": True})


@app.route("/api/tracker/clients/<client_id>/reorder", methods=["PUT"])
@tracker_required
@tool_enabled("tracker")
def api_tracker_reorder_steps(client_id):
    data = request.get_json() or {}
    step_ids = data.get("step_ids", [])
    if not step_ids:
        return jsonify({"error": "step_ids required."}), 400
    tracker_db.reorder_steps(client_id, step_ids)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Public client lookup
# ---------------------------------------------------------------------------

@app.route("/api/tracker/lookup", methods=["POST"])
@cross_origin(origins=[
    "https://ep-intelligence.com", "https://www.ep-intelligence.com",
    "http://localhost:*", "http://127.0.0.1:*",
])
@limiter.limit("10/minute")
def api_tracker_lookup():
    data = request.get_json() or {}
    firm_slug = (data.get("firm") or "").strip()
    client_id_code = (data.get("client_id") or "").strip()
    access_code = (data.get("access_code") or "").strip()
    if not firm_slug or not client_id_code or not access_code:
        return jsonify({"error": "Firm, Client ID, and Access Code are required."}), 400
    result = tracker_db.lookup_client(firm_slug, client_id_code, access_code)
    if result and result.get("locked"):
        return jsonify({"error": "Too many failed attempts. Please try again in a few minutes."}), 429
    if not result:
        return jsonify({"error": "Invalid Client ID or Access Code."}), 401
    return jsonify(result)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

REQUIRED_CONFIG_KEYS = [
    "firm_context", "ep_schema",
    "prospect_schema", "doc_separator_rules", "doc_filename_format",
    "tracker_default_steps",
]

@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_firms"))
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if ADMIN_PASSWORD and pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_firms"))
        error = "Invalid password"
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_firms():
    firms = tracker_db.list_firms() if tracker_db.DATABASE_URL else []
    return render_template("admin_firms.html", firms=firms)


@app.route("/admin/firms/new", methods=["GET", "POST"])
@admin_required
def admin_firm_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        slug = request.form.get("slug", "").strip()
        access_code = request.form.get("access_code", "").strip()
        tracker_code = request.form.get("tracker_access_code", "").strip()

        if not all([name, slug, access_code, tracker_code]):
            return render_template("admin_firm_edit.html", firm=None,
                                   error="All identity fields are required.")

        try:
            config = _parse_config_from_form(request.form)
        except ConfigParseError as e:
            return render_template("admin_firm_edit.html", firm=None, error=str(e))

        errors = _validate_config(config)
        if errors:
            stub = {"name": name, "slug": slug, "config": config}
            return render_template("admin_firm_edit.html", firm=stub,
                                   error=" | ".join(errors))

        try:
            tracker_db.create_firm(name, slug, access_code, tracker_code, config)
        except Exception as e:
            if "unique" in str(e).lower():
                stub = {"name": name, "slug": slug, "config": config}
                return render_template("admin_firm_edit.html", firm=stub,
                                       error="A firm with that slug already exists.")
            raise

        return redirect(url_for("admin_firms"))

    return render_template("admin_firm_edit.html", firm=None)


@app.route("/admin/firms/<firm_id>/duplicate")
@admin_required
def admin_firm_duplicate(firm_id):
    source = tracker_db.get_firm(firm_id)
    if not source:
        return redirect(url_for("admin_firms"))
    stub = {
        "name": "",
        "slug": "",
        "config": source.get("config") or {},
    }
    return render_template("admin_firm_edit.html", firm=stub, duplicate=True)


@app.route("/admin/firms/<firm_id>", methods=["GET", "POST"])
@admin_required
def admin_firm_edit(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        slug = request.form.get("slug", "").strip()
        access_code = request.form.get("access_code", "").strip() or None
        tracker_code = request.form.get("tracker_access_code", "").strip() or None

        if not name or not slug:
            return render_template("admin_firm_edit.html", firm=firm,
                                   error="Firm name and slug are required.")

        try:
            config = _parse_config_from_form(request.form)
        except ConfigParseError as e:
            return render_template("admin_firm_edit.html", firm=firm, error=str(e))

        errors = _validate_config(config)
        if errors:
            firm["config"] = config
            return render_template("admin_firm_edit.html", firm=firm,
                                   error=" | ".join(errors))

        try:
            tracker_db.update_firm(firm_id, name=name, slug=slug,
                                   access_code=access_code,
                                   tracker_access_code=tracker_code,
                                   config=config)
        except Exception as e:
            if "unique" in str(e).lower():
                return render_template("admin_firm_edit.html", firm=firm,
                                       error="A firm with that slug already exists.")
            raise

        with _firm_config_lock:
            _firm_config_cache.pop(str(firm_id), None)

        return redirect(url_for("admin_firms"))

    return render_template("admin_firm_edit.html", firm=firm)


class ConfigParseError(Exception):
    pass


def _parse_config_from_form(form):
    config = {}
    config["firm_context"] = form.get("firm_context", "").strip()
    config["doc_separator_rules"] = form.get("doc_separator_rules", "").strip()
    config["doc_filename_format"] = form.get("doc_filename_format", "").strip()
    config["client_site_url"] = form.get("client_site_url", "").strip()
    config["tools_enabled"] = {
        "drafting_notes": bool(form.get("tool_drafting_notes")),
        "doc_separator": bool(form.get("tool_doc_separator")),
        "prospect_summarizer": bool(form.get("tool_prospect_summarizer")),
        "tracker": bool(form.get("tool_tracker")),
    }

    for key in ["ep_schema", "prospect_schema", "tracker_default_steps"]:
        raw = form.get(key, "").strip()
        if raw:
            try:
                config[key] = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ConfigParseError(f"Invalid JSON in {key}: {e}")
        else:
            config[key] = {}

    return config


def _validate_config(config):
    errors = []
    if not config.get("firm_context"):
        errors.append("Firm context is required")
    ep = config.get("ep_schema")
    if not ep or not isinstance(ep, dict) or not ep.get("sections"):
        errors.append("EP schema must be valid JSON with a 'sections' array")
    ps = config.get("prospect_schema")
    if not ps or not isinstance(ps, dict) or not ps.get("sections"):
        errors.append("Prospect schema must be valid JSON with a 'sections' array")
    ts = config.get("tracker_default_steps")
    if not ts or not isinstance(ts, list):
        errors.append("Tracker default steps must be a JSON array")
    if not config.get("doc_filename_format"):
        errors.append("Document filename format is required")
    return errors


@app.errorhandler(404)
def page_not_found(e):
    return render_template("error.html",
                           error_code=404,
                           error_title="Page not found",
                           error_detail="The page you're looking for doesn't exist or has been moved."), 404


_error_alert_timestamps = collections.deque()
_ERROR_ALERT_MAX = 20
_ERROR_ALERT_WINDOW = 3600


def _can_send_alert():
    now = time.time()
    while _error_alert_timestamps and _error_alert_timestamps[0] < now - _ERROR_ALERT_WINDOW:
        _error_alert_timestamps.popleft()
    if len(_error_alert_timestamps) >= _ERROR_ALERT_MAX:
        return False
    _error_alert_timestamps.append(now)
    return True


def _send_error_alert(e):
    if not RESEND_API_KEY or not _can_send_alert():
        return

    firm_name = session.get("firm_name", "N/A")
    firm_slug = session.get("firm_slug", "N/A")
    tb = traceback.format_exception(type(e), e, e.__traceback__)

    body = (
        f"<h2>500 Internal Server Error</h2>"
        f"<p><strong>Time:</strong> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</p>"
        f"<p><strong>URL:</strong> {request.method} {request.url}</p>"
        f"<p><strong>Firm:</strong> {firm_name} ({firm_slug})</p>"
        f"<p><strong>IP:</strong> {request.remote_addr}</p>"
        f"<p><strong>User-Agent:</strong> {request.headers.get('User-Agent', 'N/A')}</p>"
        f"<hr>"
        f"<pre style=\"font-size:12px;white-space:pre-wrap;\">{''.join(tb)}</pre>"
    )

    try:
        resend.Emails.send({
            "from": "EP Intelligence <notifications@ep-intelligence.com>",
            "to": ["ben@ep-intelligence.com"],
            "subject": f"[500] {request.method} {request.path}",
            "html": body,
        })
    except Exception:
        app.logger.warning("Failed to send 500 alert email", exc_info=True)


def _notify_tool_error(tool_name, error, firm_id=None, firm_name=None, firm_slug=None):
    """Send alert for tool errors that don't trigger the 500 error handler."""
    if not RESEND_API_KEY or not _can_send_alert():
        return
    if firm_name is None:
        firm_name = session.get("firm_name", "N/A") if has_request_context() else "N/A"
    if firm_slug is None:
        firm_slug = session.get("firm_slug", "N/A") if has_request_context() else "N/A"

    tb = traceback.format_exc()
    req_url = f"{request.method} {request.url}" if has_request_context() else "background job"

    body = (
        f"<h2>Tool Error: {tool_name}</h2>"
        f"<p><strong>Time:</strong> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</p>"
        f"<p><strong>URL:</strong> {req_url}</p>"
        f"<p><strong>Firm:</strong> {firm_name} ({firm_slug})</p>"
        f"<p><strong>Error:</strong> {error}</p>"
        f"<hr>"
        f"<pre style=\"font-size:12px;white-space:pre-wrap;\">{tb}</pre>"
    )

    try:
        resend.Emails.send({
            "from": "EP Intelligence <notifications@ep-intelligence.com>",
            "to": ["ben@ep-intelligence.com"],
            "subject": f"[Tool Error] {tool_name}",
            "html": body,
        })
    except Exception:
        app.logger.warning("Failed to send tool error alert email", exc_info=True)


@app.errorhandler(500)
def internal_server_error(e):
    _send_error_alert(e)
    return render_template("error.html",
                           error_code=500,
                           error_title="Something went wrong",
                           error_detail="Please try again, or contact support if the issue persists."), 500


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", "8080")))
