import collections
import html
import io
import json
import os
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, Response, has_request_context, jsonify, make_response, redirect, render_template, request, session, url_for
from flask_cors import cross_origin
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from openai import OpenAI

from ai_logger import log_ai_call, extract_xai_usage, completion_details, log_context
from doc_separator import separate_documents, redo_with_feedback, _extract_json
from ep_export import build_export_csv, build_questionnaire_docx
from prospect_summarizer import extract_prospect_documents, build_summary_docx, PROSPECT_SCHEMA
from quote_verify import verify_quotes
import resend
import stripe
import tracker_db
import usage_tools

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=10)

_EASTERN = ZoneInfo("America/New_York")


def _to_eastern(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_EASTERN)


@app.template_filter("et_time")
def _et_time_filter(dt):
    eastern = _to_eastern(dt)
    if eastern is None:
        return ""
    return eastern.strftime("%b %d, %Y %I:%M %p ET")


@app.template_filter("relative_time")
def _relative_time_filter(dt):
    eastern = _to_eastern(dt)
    if eastern is None:
        return ""
    now = datetime.now(_EASTERN)
    seconds = (now - eastern).total_seconds()
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours}h ago"
    today = now.date()
    day = eastern.date()
    if day == today - timedelta(days=1):
        return "Yesterday"
    days = int(seconds // 86400)
    if days < 7:
        return f"{days}d ago"
    if day.year == today.year:
        return eastern.strftime("%b ") + str(eastern.day)
    return eastern.strftime("%b ") + str(eastern.day) + ", " + str(eastern.year)


def _et_time_csv(dt):
    eastern = _to_eastern(dt)
    if eastern is None:
        return ""
    return eastern.strftime("%Y-%m-%d %H:%M:%S")


limiter = Limiter(get_remote_address, app=app)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

openai_client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    timeout=int(os.environ.get("OPENAI_TIMEOUT", "120")),
)

if tracker_db.DATABASE_URL:
    tracker_db.init_db()
    tracker_db.seed_firm_if_empty()


# ---------------------------------------------------------------------------
# Firm config helpers
# ---------------------------------------------------------------------------

_firm_config_cache: dict = {}
_employee_req_cache: dict = {}
_feedback_widget_cache: dict = {}
_firm_config_lock = threading.Lock()
_CACHE_TTL = 300  # 5 minutes
_AUTH_FLAG_TTL = 30  # seconds; logout after toggle-on may lag by this much


def _invalidate_firm_cache(firm_id):
    key = str(firm_id)
    with _firm_config_lock:
        _firm_config_cache.pop(key, None)
        _employee_req_cache.pop(key, None)
        _feedback_widget_cache.pop(key, None)


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


def _firm_requires_employee_login(firm_id):
    """Cached require_employee_login flag. None if the firm no longer exists."""
    key = str(firm_id)
    now = time.time()
    with _firm_config_lock:
        cached = _employee_req_cache.get(key)
        if cached and now - cached["ts"] < _AUTH_FLAG_TTL:
            return cached["value"]

    required = tracker_db.firm_requires_employee_login(key)
    with _firm_config_lock:
        _employee_req_cache[key] = {"value": required, "ts": now}
    return required


def _firm_feedback_widget_enabled(firm_id):
    """Cached feedback_widget_enabled flag. False if missing, firm gone, or lookup fails."""
    if not tracker_db.DATABASE_URL or not firm_id:
        return False
    key = str(firm_id)
    now = time.time()
    with _firm_config_lock:
        cached = _feedback_widget_cache.get(key)
        if cached and now - cached["ts"] < _AUTH_FLAG_TTL:
            return bool(cached["value"])

    try:
        enabled = tracker_db.firm_feedback_widget_enabled(key)
    except Exception:
        app.logger.warning("feedback widget flag lookup failed", exc_info=True)
        return False
    with _firm_config_lock:
        _feedback_widget_cache[key] = {"value": enabled, "ts": now}
    return bool(enabled)


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
        "connect-src 'self' https://api.stripe.com https://merchant-ui-api.stripe.com; "
        "img-src 'self' data: https://*.stripe.com; "
        "frame-src https://js.stripe.com https://hooks.stripe.com https://connect-js.stripe.com; "
        "frame-ancestors 'none'"
    )
    return response


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------

def _clear_firm_session():
    for key in (
        "authenticated", "firm_id", "firm_name", "firm_slug",
        "tracker_authenticated", "employee_id", "employee_code", "employee_name",
        "pending_firm_id", "pending_firm_name",
    ):
        session.pop(key, None)


def _clear_pending_login():
    session.pop("pending_firm_id", None)
    session.pop("pending_firm_name", None)


def _session_log_ctx():
    eid = session.get("employee_id")
    if not eid:
        return {}
    return {
        "employee_id": eid,
        "employee_id_code": session.get("employee_code"),
        "employee_name": session.get("employee_name"),
    }


def _employee_session_valid():
    if not session.get("authenticated"):
        return False
    if not tracker_db.DATABASE_URL:
        return True
    firm_id = session.get("firm_id")
    if not firm_id:
        return False
    required = _firm_requires_employee_login(firm_id)
    if required is None:
        return False
    if not required:
        return True
    return bool(session.get("employee_id"))


def _clear_usage_dash_session():
    for key in ("usage_dash", "usage_dash_firm_id", "usage_dash_firm_name"):
        session.pop(key, None)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        if not _employee_session_valid():
            _clear_firm_session()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def tracker_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        if not _employee_session_valid():
            _clear_firm_session()
            return jsonify({"error": "Not authenticated"}), 401
        if not session.get("tracker_authenticated"):
            return jsonify({"error": "Tracker access required"}), 403
        return f(*args, **kwargs)
    return decorated


_OPT_IN_TOOLS = {"doc_differences", "estate_tax_calc"}


def _is_tool_enabled(tool_key):
    """Check whether a tool is enabled for the current firm."""
    firm_id = session.get("firm_id")
    if not firm_id:
        return True
    config = _get_firm_config(firm_id) or {}
    default = tool_key not in _OPT_IN_TOOLS
    return config.get("tools_enabled", {}).get(tool_key, default)


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


@app.context_processor
def _inject_feedback_widget():
    try:
        if not has_request_context():
            return {"show_feedback_widget": False}
        path = request.path or ""
        if path.startswith("/admin") or path.startswith("/firm-usage"):
            return {"show_feedback_widget": False}
        if not _employee_session_valid():
            return {"show_feedback_widget": False}
        firm_id = session.get("firm_id")
        if not firm_id or not _firm_feedback_widget_enabled(firm_id):
            return {"show_feedback_widget": False}
        return {"show_feedback_widget": True}
    except Exception:
        app.logger.warning("feedback widget inject failed", exc_info=True)
        return {"show_feedback_widget": False}


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/agreement")
def agreement():
    return render_template("agreement.html")


@app.route("/custom-tool-agreement")
def custom_tool_agreement():
    return render_template("custom_tool_agreement.html")


CUSTOM_TOOL_AGREEMENT_VERSION = "August 19, 2026"
TTL_FIRM_NAME = "Texas Trust Law"


@app.route("/engagement/texas-trust-law", methods=["GET", "POST"])
@limiter.limit("5/minute")
def ttl_engagement():
    if request.method == "GET":
        return render_template(
            "ttl_engagement.html",
            agreement_version=CUSTOM_TOOL_AGREEMENT_VERSION,
        )

    signer_name = request.form.get("signer_name", "").strip()
    title = request.form.get("title", "").strip()
    email = request.form.get("email", "").strip()
    agreed = request.form.get("agree") == "on"

    if not all([signer_name, title, email, agreed]):
        return render_template(
            "ttl_engagement.html",
            agreement_version=CUSTOM_TOOL_AGREEMENT_VERSION,
            error="Please complete all required fields and accept the Agreement.",
        )

    accepted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ip_address = request.remote_addr or ""
    user_agent = request.headers.get("User-Agent", "")

    def _e(value):
        return html.escape(value or "")

    record = (
        f"<h2>Custom Tool Engagement Accepted</h2>"
        f"<p><strong>Firm:</strong> {_e(TTL_FIRM_NAME)}</p>"
        f"<p><strong>Signer:</strong> {_e(signer_name)}</p>"
        f"<p><strong>Title:</strong> {_e(title)}</p>"
        f"<p><strong>Email:</strong> {_e(email)}</p>"
        f"<p><strong>Agreement version:</strong> {_e(CUSTOM_TOOL_AGREEMENT_VERSION)}</p>"
        f"<p><strong>Accepted at:</strong> {_e(accepted_at)}</p>"
        f"<p><strong>IP address:</strong> {_e(ip_address)}</p>"
        f"<p><strong>User agent:</strong> {_e(user_agent)}</p>"
        f"<p>The signer checked the box agreeing to the EPI Custom Tool Development "
        f"&amp; Access Agreement and represented authority to bind the Firm.</p>"
    )

    if RESEND_API_KEY:
        try:
            resend.Emails.send({
                "from": "EP Intelligence <notifications@ep-intelligence.com>",
                "to": ["ben@ep-intelligence.com"],
                "subject": f"Engagement accepted: {TTL_FIRM_NAME} — {signer_name}",
                "html": record,
            })
        except Exception:
            traceback.print_exc()
            return render_template(
                "ttl_engagement.html",
                agreement_version=CUSTOM_TOOL_AGREEMENT_VERSION,
                error="We could not record your acceptance. Please try again or email ben@ep-intelligence.com.",
            )
        try:
            resend.Emails.send({
                "from": "EP Intelligence <notifications@ep-intelligence.com>",
                "to": [email],
                "subject": f"Engagement letter — {TTL_FIRM_NAME} / Estate Planning Intelligence LLC",
                "html": (
                    f"<p>This confirms that {_e(signer_name)}, {_e(title)}, accepted the "
                    f"EPI Custom Tool Development &amp; Access Agreement "
                    f"({_e(CUSTOM_TOOL_AGREEMENT_VERSION)}) on behalf of {_e(TTL_FIRM_NAME)} "
                    f"on {_e(accepted_at)}.</p>"
                    f"<p>A copy of the Agreement is at "
                    f"<a href=\"https://ep-intelligence.com/custom-tool-agreement\">"
                    f"ep-intelligence.com/custom-tool-agreement</a>.</p>"
                ),
            })
        except Exception:
            traceback.print_exc()
    else:
        app.logger.warning("RESEND_API_KEY not set — engagement acceptance email not sent")

    return render_template(
        "ttl_engagement.html",
        agreement_version=CUSTOM_TOOL_AGREEMENT_VERSION,
        accepted=True,
        signer_name=signer_name,
        title=title,
        email=email,
        accepted_at=accepted_at,
    )


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
        if request.args.get("session_id"):
            return render_template("onboarding.html", step="success")
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


@app.route("/api/create-checkout-session", methods=["POST"])
@limiter.limit("5/minute")
def create_checkout_session():
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return jsonify({"error": "Payment not configured."}), 503
    try:
        checkout_session = stripe.checkout.Session.create(
            ui_mode="embedded_page",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            mode="subscription",
            return_url=request.host_url + "onboarding?session_id={CHECKOUT_SESSION_ID}",
        )
        return jsonify({"clientSecret": checkout_session.client_secret})
    except Exception as e:
        app.logger.error("Stripe checkout session error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login():
    if session.get("authenticated"):
        if _employee_session_valid():
            return redirect(url_for("dashboard"))
        _clear_firm_session()

    if request.method == "GET":
        _clear_pending_login()
        return render_template("login.html", error=None)

    code = request.form.get("access_code", "")
    firm = tracker_db.lookup_firm_by_access_code(code) if code else None
    if not firm:
        if tracker_db.DATABASE_URL:
            tracker_db.log_login_attempt(
                ip_address=request.remote_addr or "",
                access_code_used=code,
                firm_name=None,
                firm_slug=None,
                success=False,
            )
        return render_template("login.html", error="Invalid access code")

    if not firm.get("require_employee_login"):
        _clear_pending_login()
        if tracker_db.DATABASE_URL:
            tracker_db.log_login_attempt(
                ip_address=request.remote_addr or "",
                access_code_used=code,
                firm_name=firm["name"],
                firm_slug=firm["slug"],
                success=True,
            )
        session.permanent = True
        session["authenticated"] = True
        session["firm_id"] = str(firm["id"])
        session["firm_name"] = firm["name"]
        session["firm_slug"] = firm["slug"]
        session.pop("employee_id", None)
        session.pop("employee_code", None)
        session.pop("employee_name", None)
        return redirect(url_for("dashboard"))

    session["pending_firm_id"] = str(firm["id"])
    session["pending_firm_name"] = firm["name"]
    return redirect(url_for("login_employee"))


@app.route("/login/employee", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login_employee():
    if session.get("authenticated"):
        if _employee_session_valid():
            return redirect(url_for("dashboard"))
        _clear_firm_session()

    pending_id = session.get("pending_firm_id")
    if not pending_id:
        return redirect(url_for("login"))

    firm = tracker_db.get_firm(pending_id)
    if not firm or not firm.get("require_employee_login"):
        _clear_pending_login()
        return redirect(url_for("login"))

    firm_name = firm["name"]
    hint = (firm.get("employee_login_hint") or "").strip()
    if request.method == "GET":
        return render_template("login_employee.html", firm_name=firm_name,
                               employee_login_hint=hint)

    employee_code = (request.form.get("employee_id") or "").strip()
    if not employee_code:
        return render_template("login_employee.html", firm_name=firm_name,
                               employee_login_hint=hint,
                               error="Employee ID is required.")

    employee = tracker_db.get_employee_by_code(str(firm["id"]), employee_code)
    if not employee:
        if tracker_db.DATABASE_URL:
            tracker_db.log_login_attempt(
                ip_address=request.remote_addr or "",
                access_code_used="",
                firm_name=firm["name"],
                firm_slug=firm["slug"],
                success=False,
            )
        return render_template("login_employee.html", firm_name=firm_name,
                               employee_login_hint=hint,
                               error="Invalid employee ID")

    _clear_pending_login()
    if tracker_db.DATABASE_URL:
        tracker_db.log_login_attempt(
            ip_address=request.remote_addr or "",
            access_code_used="",
            firm_name=firm["name"],
            firm_slug=firm["slug"],
            success=True,
            employee_id_code=employee["employee_id_code"],
            employee_name=employee["name"],
        )
    session.permanent = True
    session["authenticated"] = True
    session["firm_id"] = str(firm["id"])
    session["firm_name"] = firm["name"]
    session["firm_slug"] = firm["slug"]
    session["employee_id"] = str(employee["id"])
    session["employee_code"] = employee["employee_id_code"]
    session["employee_name"] = employee["name"]
    return redirect(url_for("dashboard"))


def _firm_usage_days_from_request():
    days_raw = (request.args.get("days") or "30").strip()
    if days_raw == "all":
        return None, "all"
    if days_raw in ("1", "7", "30", "90"):
        return int(days_raw), days_raw
    return 30, "30"


def _firm_usage_person_key(row):
    return (
        str(row.get("employee_id") or ""),
        row.get("employee_id_code") or "",
        row.get("employee_name") or "",
    )


def _render_firm_usage_dashboard():
    firm_id = session.get("usage_dash_firm_id")
    if not firm_id:
        _clear_usage_dash_session()
        return redirect(url_for("firm_usage"))
    days, days_key = _firm_usage_days_from_request()
    overview = {
        "calls": 0, "last_seen": None, "people": 0, "tools": 0,
        "cost": 0, "cost_openai": 0, "cost_ocr": 0,
    }
    people = []
    tools = []

    if tracker_db.DATABASE_URL:
        row = tracker_db.firm_usage_overview(firm_id, days=days)
        if row:
            overview["calls"] = int(row.get("calls") or 0)
            overview["last_seen"] = row.get("last_seen")
            overview["tools"] = int(row.get("tools") or 0)
            overview["cost"] = float(row.get("cost") or 0)
            overview["cost_openai"] = float(row.get("cost_openai") or 0)
            overview["cost_ocr"] = float(row.get("cost_ocr") or 0)

        events_by_tool = {}
        for event in tracker_db.firm_usage_recent_events(firm_id, days=days):
            events_by_tool.setdefault(event.get("tool"), []).append(dict(event))

        for raw in tracker_db.firm_usage_by_tool(firm_id, days=days):
            tool_key = raw.get("tool")
            events = events_by_tool.get(tool_key, [])
            tools.append({
                "tool": tool_key,
                "label": _usage_label_tool(tool_key),
                "calls": int(raw.get("calls") or 0),
                "last_seen": raw.get("last_seen"),
                "events": events,
                "events_capped": int(raw.get("calls") or 0) > len(events),
            })

        tools_by_person = {}
        for t in tracker_db.firm_usage_tools_by_employee(firm_id, days=days):
            key = _firm_usage_person_key(t)
            tools_by_person.setdefault(key, []).append({
                "label": _usage_label_tool(t.get("tool")),
                "calls": int(t.get("calls") or 0),
            })

        seen = set()
        for raw in tracker_db.firm_usage_by_employee(firm_id, days=days):
            item = dict(raw)
            key = _firm_usage_person_key(item)
            seen.add(item.get("employee_id_code") or "")
            item["calls"] = int(item.get("calls") or 0)
            item["tools"] = tools_by_person.get(key, [])
            people.append(item)

        for emp in tracker_db.list_employees(firm_id):
            code = emp.get("employee_id_code") or ""
            if code in seen:
                continue
            people.append({
                "employee_id": emp.get("id"),
                "employee_id_code": code,
                "employee_name": emp.get("name"),
                "calls": 0,
                "last_seen": None,
                "tools": [],
            })

        people.sort(key=lambda p: (-int(p.get("calls") or 0), (p.get("employee_name") or "").lower()))
        overview["people"] = sum(1 for p in people if int(p.get("calls") or 0) > 0)

    chart = _firm_usage_chart_series(firm_id, days)
    show_costs = bool(tracker_db.firm_usage_dashboard_show_costs(firm_id))
    cost_series = _usage_cost_series(days, firm_id, None) if show_costs else []

    return render_template(
        "firm_usage.html",
        firm_name=session.get("usage_dash_firm_name", ""),
        overview=overview,
        people=people,
        tools=tools,
        chart=chart,
        show_costs=show_costs,
        cost_series=cost_series,
        days_key=days_key,
    )


@app.route("/firm-usage", methods=["GET"], strict_slashes=False)
def firm_usage():
    firm_id = session.get("usage_dash_firm_id")
    if session.get("usage_dash") and firm_id:
        if tracker_db.DATABASE_URL and tracker_db.firm_usage_dashboard_enabled(firm_id):
            return _render_firm_usage_dashboard()
        _clear_usage_dash_session()
    return render_template("firm_usage_login.html", error=None)


@app.route("/firm-usage/login", methods=["GET", "POST"], strict_slashes=False)
@limiter.limit("10/minute")
def firm_usage_login():
    if request.method == "GET":
        return redirect(url_for("firm_usage"))
    access_code = request.form.get("access_code", "")
    password = request.form.get("dashboard_password", "")
    firm = tracker_db.authenticate_usage_dashboard(access_code, password) if tracker_db.DATABASE_URL else None
    if not firm:
        return render_template(
            "firm_usage_login.html",
            error="Invalid access code or password",
        )
    session.permanent = True
    session["usage_dash"] = True
    session["usage_dash_firm_id"] = str(firm["id"])
    session["usage_dash_firm_name"] = firm["name"]
    return redirect(url_for("firm_usage"))


@app.route("/firm-usage/logout", strict_slashes=False)
def firm_usage_logout():
    _clear_usage_dash_session()
    return redirect(url_for("firm_usage"))


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
# Estate Tax Calculator
# ---------------------------------------------------------------------------

@app.route("/estate-tax-calculator")
@login_required
@tool_enabled("estate_tax_calc")
def estate_tax_calculator():
    return render_template("estate_tax_calc.html", firm_name=session.get("firm_name", ""))



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
    raw = ""
    details = None
    try:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        details = completion_details(response, raw)
        extraction = _extract_json(raw)
        verify_quotes(extraction, transcript)
        log_ai_call(
            provider="openai", model=OPENAI_MODEL, tool="ep_extract", status="success",
            execution_ms=int((time.time() - call_start) * 1000),
            firm_id=firm_id,
            **extract_xai_usage(response),
        )
        extraction["schema"] = ep_schema["sections"]
        return jsonify(extraction)
    except Exception as e:
        log_ai_call(
            provider="openai", model=OPENAI_MODEL, tool="ep_extract", status="error",
            execution_ms=int((time.time() - call_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        if details:
            e.details = details
        app.logger.error("EP extract error: %s", e)
        _notify_tool_error("Drafting Notes", str(e), firm_id=firm_id,
                           details=getattr(e, "details", None))
        if isinstance(e, (json.JSONDecodeError, ValueError)) and "JSON" in str(e):
            return jsonify({"error": "AI returned invalid JSON. Please try again."}), 500
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


def _run_doc_separate(job_id, pdf_content, firm_id, firm_config, log_ctx=None):
    with log_context(**(log_ctx or {})):
        try:
            zip_buf, documents, page_texts, total_pages = separate_documents(
                pdf_content, openai_client, firm_id=firm_id, firm_config=firm_config,
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
                               firm_slug=_jobs[job_id].get("firm_slug"),
                               details=getattr(e, "details", None))
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
    log_ctx = _session_log_ctx()

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _purge_stale(_jobs)
        _purge_stale(_zip_cache)
        _jobs[job_id] = {"status": "processing", "ts": time.time(),
                         "firm_name": session.get("firm_name"),
                         "firm_slug": session.get("firm_slug"),
                         "log_ctx": log_ctx}

    _executor.submit(_run_doc_separate, job_id, pdf_content, firm_id, firm_config, log_ctx)
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
                           previous_documents, feedback, firm_id, firm_config,
                           log_ctx=None):
    with log_context(**(log_ctx or {})):
        try:
            zip_buf, documents = redo_with_feedback(
                pdf_content, openai_client, page_texts, total_pages,
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
                               firm_slug=_jobs[job_id].get("firm_slug"),
                               details=getattr(e, "details", None))
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

    log_ctx = original_job.get("log_ctx") or _session_log_ctx()
    new_job_id = uuid.uuid4().hex
    with _jobs_lock:
        _purge_stale(_jobs)
        _purge_stale(_zip_cache)
        _jobs[new_job_id] = {"status": "processing", "ts": time.time(),
                             "firm_name": session.get("firm_name"),
                             "firm_slug": session.get("firm_slug"),
                             "log_ctx": log_ctx}

    _executor.submit(
        _run_doc_separate_redo, new_job_id, pdf_content, page_texts,
        total_pages, previous_documents, feedback, firm_id, firm_config,
        log_ctx,
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


def _run_prospect_summarize(job_id, pdf_contents, notes, firm_id, firm_config, log_ctx=None):
    with log_context(**(log_ctx or {})):
        try:
            prospect_schema = _get_prospect_schema(firm_config)
            extraction, ocr_text = extract_prospect_documents(
                pdf_contents, openai_client, notes=notes,
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
                               firm_slug=_jobs[job_id].get("firm_slug"),
                               details=getattr(e, "details", None))
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
    log_ctx = _session_log_ctx()

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _purge_stale(_jobs)
        _jobs[job_id] = {"status": "processing", "ts": time.time(),
                         "firm_name": session.get("firm_name"),
                         "firm_slug": session.get("firm_slug"),
                         "log_ctx": log_ctx}

    _executor.submit(_run_prospect_summarize, job_id, pdf_contents, notes, firm_id, firm_config, log_ctx)
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
# Document Differences
# ---------------------------------------------------------------------------

@app.route("/doc-differences")
@login_required
@tool_enabled("doc_differences")
def doc_differences():
    return render_template("doc_differences.html", firm_name=session.get("firm_name", ""))


@app.route("/api/doc-differences", methods=["POST"])
@login_required
@tool_enabled("doc_differences")
def api_doc_differences():
    from doc_differences import rank_specimens

    f = request.files.get("docx")
    if not f or not f.filename.lower().endswith(".docx"):
        return jsonify({"error": "Please upload a .docx file."}), 400

    upload_bytes = f.read()
    if not upload_bytes:
        return jsonify({"error": "The uploaded file is empty."}), 400

    firm_id = session.get("firm_id")
    specimens = tracker_db.list_specimen_documents(firm_id)
    if not specimens:
        return jsonify({"error": "No specimen documents configured for your firm. Contact your administrator."}), 400

    full_specimens = []
    for spec in specimens:
        full = tracker_db.get_specimen_document(spec["id"], firm_id=firm_id)
        if full:
            full_specimens.append(full)

    try:
        rankings = rank_specimens(upload_bytes, full_specimens)
    except Exception as e:
        app.logger.error("Doc differences ranking error: %s", e)
        _notify_tool_error("Document Differences", str(e))
        return jsonify({"error": "Failed to process document."}), 500

    upload_name = f.filename.rsplit(".", 1)[0] if f.filename else "Document"

    token = uuid.uuid4().hex
    with _jobs_lock:
        _purge_stale(_zip_cache)
        _zip_cache[token] = {"data": upload_bytes, "name": upload_name, "ts": time.time()}

    return jsonify({"rankings": rankings, "upload_token": token, "upload_name": upload_name})


@app.route("/api/doc-differences/export", methods=["POST"])
@login_required
@tool_enabled("doc_differences")
def api_doc_differences_export():
    from doc_differences import build_diff_docx

    data = request.get_json() or {}
    specimen_id = data.get("specimen_id", "").strip()
    upload_token = data.get("upload_token", "").strip()
    if not specimen_id or not upload_token:
        return jsonify({"error": "specimen_id and upload_token are required."}), 400

    with _jobs_lock:
        cached = _zip_cache.get(upload_token)
    if not cached:
        return jsonify({"error": "Upload expired. Please re-upload the document."}), 410
    upload_bytes = cached["data"]
    upload_name = cached.get("name", "Document")

    firm_id = session.get("firm_id")
    specimen = tracker_db.get_specimen_document(specimen_id, firm_id=firm_id)
    if not specimen:
        return jsonify({"error": "Specimen not found."}), 404

    try:
        buf = build_diff_docx(upload_bytes, specimen["docx_data"])
    except Exception as e:
        app.logger.error("Doc differences export error: %s", e)
        _notify_tool_error("Document Differences", str(e))
        return jsonify({"error": str(e)}), 500

    from urllib.parse import quote
    filename = f"{upload_name} vs. {specimen['name']}.docx"
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


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
# Feedback widget
# ---------------------------------------------------------------------------

_FEEDBACK_MAX_LEN = 4000
_FEEDBACK_TYPE_LABELS = tracker_db.FEEDBACK_TYPE_LABELS


def _sanitize_feedback_path(raw):
    path = (raw or "").strip()
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        return ""
    if "\\" in path or "\n" in path or "\r" in path:
        return ""
    return path[:200]


def _send_feedback_email(payload):
    if not RESEND_API_KEY:
        app.logger.warning("RESEND_API_KEY not set — feedback email not sent")
        return

    def _e(value):
        return html.escape(str(value) if value is not None else "")

    firm_name = payload.get("firm_name") or "Unknown firm"
    employee = payload.get("employee_name") or "—"
    emp_code = payload.get("employee_id_code")
    if emp_code:
        employee = f"{employee} ({emp_code})" if payload.get("employee_name") else emp_code
    type_label = _FEEDBACK_TYPE_LABELS.get(payload.get("feedback_type"), payload.get("feedback_type") or "—")
    created = payload.get("created_at")
    time_str = _et_time_filter(created) if created else datetime.now(_EASTERN).strftime("%b %d, %Y %I:%M %p ET")

    body = (
        f"<h2>New Feedback</h2>"
        f"<p><strong>Time:</strong> {_e(time_str)}</p>"
        f"<p><strong>Firm:</strong> {_e(firm_name)}"
        f"{' (' + _e(payload.get('firm_slug')) + ')' if payload.get('firm_slug') else ''}</p>"
        f"<p><strong>Employee:</strong> {_e(employee)}</p>"
        f"<p><strong>Page:</strong> {_e(payload.get('page_path') or '—')}</p>"
        f"<p><strong>Type:</strong> {_e(type_label)}</p>"
        f"<p><strong>IP:</strong> {_e(payload.get('ip_address') or '—')}</p>"
        f"<p><strong>User-Agent:</strong> {_e(payload.get('user_agent') or '—')}</p>"
        f"<hr>"
        f"<pre style=\"font-size:13px;white-space:pre-wrap;\">{_e(payload.get('message'))}</pre>"
    )
    try:
        resend.Emails.send({
            "from": "EP Intelligence <notifications@ep-intelligence.com>",
            "to": ["ben@ep-intelligence.com"],
            "subject": f"[Feedback] {firm_name}",
            "html": body,
        })
    except Exception:
        app.logger.warning("Failed to send feedback email", exc_info=True)


@app.route("/api/feedback", methods=["POST"])
@limiter.limit("10/hour")
def api_feedback():
    if not session.get("authenticated") or not _employee_session_valid():
        return jsonify({"error": "Not authenticated"}), 401
    firm_id = session.get("firm_id")
    if not firm_id or not _firm_feedback_widget_enabled(firm_id):
        return jsonify({"error": "Feedback is not enabled for your firm."}), 403

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Please enter your feedback."}), 400
    if len(message) > _FEEDBACK_MAX_LEN:
        return jsonify({"error": f"Feedback must be {_FEEDBACK_MAX_LEN} characters or fewer."}), 400

    feedback_type = (data.get("type") or "").strip().lower()
    if feedback_type not in tracker_db.FEEDBACK_TYPES:
        return jsonify({"error": "Please choose Bug, Idea, or Other."}), 400

    page_path = _sanitize_feedback_path(data.get("page"))
    user_agent = (request.headers.get("User-Agent") or "")[:500]
    ip_address = request.remote_addr or ""

    payload = {
        "firm_id": firm_id,
        "firm_name": session.get("firm_name") or "",
        "firm_slug": session.get("firm_slug") or "",
        "employee_id": session.get("employee_id"),
        "employee_id_code": session.get("employee_code"),
        "employee_name": session.get("employee_name"),
        "page_path": page_path,
        "feedback_type": feedback_type,
        "message": message,
        "user_agent": user_agent,
        "ip_address": ip_address,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        tracker_db.create_feedback(
            firm_id=payload["firm_id"],
            firm_name=payload["firm_name"],
            firm_slug=payload["firm_slug"],
            employee_id=payload["employee_id"],
            employee_id_code=payload["employee_id_code"],
            employee_name=payload["employee_name"],
            page_path=payload["page_path"],
            feedback_type=payload["feedback_type"],
            message=payload["message"],
            user_agent=payload["user_agent"],
            ip_address=payload["ip_address"],
        )
    except Exception:
        app.logger.error("Failed to store feedback", exc_info=True)
        return jsonify({"error": "Could not save feedback. Please try again."}), 500

    _send_feedback_email(payload)
    return jsonify({"ok": True})


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


@app.route("/admin/login-log")
@admin_required
def admin_login_log():
    firm_slug = request.args.get("firm", "")
    status = request.args.get("status", "")
    success_filter = None
    if status == "success":
        success_filter = True
    elif status == "failure":
        success_filter = False
    attempts = []
    if tracker_db.DATABASE_URL:
        attempts = tracker_db.get_login_attempts(
            firm_slug=firm_slug or None,
            success=success_filter,
        )
    firms = tracker_db.list_firms() if tracker_db.DATABASE_URL else []
    return render_template("admin_login_log.html", attempts=attempts, firms=firms,
                           selected_firm=firm_slug, selected_status=status)


@app.route("/admin/login-log/csv")
@admin_required
def admin_login_log_csv():
    firm_slug = request.args.get("firm", "")
    status = request.args.get("status", "")
    success_filter = None
    if status == "success":
        success_filter = True
    elif status == "failure":
        success_filter = False
    attempts = tracker_db.get_login_attempts(
        limit=10000,
        firm_slug=firm_slug or None,
        success=success_filter,
    ) if tracker_db.DATABASE_URL else []

    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Time (ET)", "IP Address", "Access Code", "Firm", "Employee ID", "Employee Name", "Result"])
    for a in attempts:
        writer.writerow([
            _et_time_csv(a["attempted_at"]),
            a["ip_address"],
            a["access_code_used"],
            a["firm_name"] or "",
            a.get("employee_id_code") or "",
            a.get("employee_name") or "",
            "Success" if a["success"] else "Failed",
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = f"attachment; filename=Login_Log_{time.strftime('%m-%d-%Y_%H%M%S')}.csv"
    return resp


def _admin_feedback_firm_id():
    firm_id = (request.args.get("firm") or "").strip() or None
    if firm_id:
        try:
            uuid.UUID(firm_id)
        except (ValueError, TypeError, AttributeError):
            firm_id = None
    return firm_id


@app.route("/admin/feedback")
@admin_required
def admin_feedback():
    firm_id = _admin_feedback_firm_id()
    if firm_id:
        return redirect(url_for("admin_firm_feedback", firm_id=firm_id))
    return redirect(url_for("admin_firms"))


@app.route("/admin/feedback/csv")
@admin_required
def admin_feedback_csv():
    firm_id = _admin_feedback_firm_id()
    if firm_id:
        return redirect(url_for("admin_firm_feedback_csv", firm_id=firm_id))
    return redirect(url_for("admin_firms"))


@app.route("/admin/firms/<firm_id>/feedback")
@admin_required
def admin_firm_feedback(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    items = tracker_db.list_feedback(firm_id=firm_id) if tracker_db.DATABASE_URL else []
    return render_template(
        "admin_feedback.html",
        items=items,
        firm=firm,
        type_labels=_FEEDBACK_TYPE_LABELS,
    )


@app.route("/admin/firms/<firm_id>/feedback/csv")
@admin_required
def admin_firm_feedback_csv(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    items = tracker_db.list_feedback(limit=10000, firm_id=firm_id) if tracker_db.DATABASE_URL else []

    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Time (ET)", "Firm", "Firm Slug", "Employee Name", "Employee ID",
        "Page", "Type", "Message", "User Agent", "IP Address",
    ])
    for row in items:
        writer.writerow([
            _et_time_csv(row.get("created_at")),
            row.get("firm_name") or "",
            row.get("firm_slug") or "",
            row.get("employee_name") or "",
            row.get("employee_id_code") or "",
            row.get("page_path") or "",
            _FEEDBACK_TYPE_LABELS.get(row.get("feedback_type"), row.get("feedback_type") or ""),
            row.get("message") or "",
            row.get("user_agent") or "",
            row.get("ip_address") or "",
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=Feedback_{time.strftime('%m-%d-%Y_%H%M%S')}.csv"
    )
    return resp


_USAGE_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "google_documentai": "Document AI",
}


def _usage_filters_from_request():
    days_raw = (request.args.get("days") or "30").strip()
    if days_raw == "all":
        days, days_key = None, "all"
    elif days_raw in ("1", "7", "30", "90"):
        days, days_key = int(days_raw), days_raw
    else:
        days, days_key = 30, "30"

    firm_id = (request.args.get("firm") or "").strip() or None
    if firm_id:
        try:
            uuid.UUID(firm_id)
        except (ValueError, TypeError, AttributeError):
            firm_id = None

    employee_code = (request.args.get("employee") or "").strip() or None
    return days, days_key, firm_id, employee_code


def _usage_error_summary(notes):
    if not notes:
        return ""
    lines = [ln.strip() for ln in str(notes).splitlines() if ln.strip()]
    if not lines:
        return ""
    summary = lines[-1]
    if len(summary) > 180:
        return summary[:177] + "..."
    return summary


def _usage_label_tool(tool):
    return usage_tools.display_label(tool)


def _usage_label_provider(provider):
    return _USAGE_PROVIDER_LABELS.get(provider, provider or "—")


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _monday(d):
    return d - timedelta(days=d.weekday())


def _as_et_hour(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_EASTERN)
    else:
        value = value.astimezone(_EASTERN)
    return value.replace(minute=0, second=0, microsecond=0)


def _clock_parts(dt):
    hour = dt.strftime("%I").lstrip("0") or "12"
    ampm = dt.strftime("%p")
    return hour, ampm


def _usage_cost_series(days, firm_id, employee_code):
    if not tracker_db.DATABASE_URL:
        return []

    if days == 1:
        raw = tracker_db.usage_cost_by_hour(days=days, firm_id=firm_id, employee_code=employee_code)
        by_hour = {}
        for row in raw:
            hour = _as_et_hour(row.get("hour"))
            if not hour:
                continue
            by_hour[hour] = float(row.get("cost") or 0)
        end = datetime.now(_EASTERN).replace(minute=0, second=0, microsecond=0)
        cursor = end - timedelta(hours=23)
        series = []
        while cursor <= end:
            hour, ampm = _clock_parts(cursor)
            series.append({
                "date": cursor.isoformat(),
                "label": hour + " " + ampm,
                "tip": (
                    cursor.strftime("%b ") + str(cursor.day) + ", " + str(cursor.year)
                    + ", " + hour + cursor.strftime(":%M ") + ampm + " ET"
                ),
                "cost": round(by_hour.get(cursor, 0.0), 4),
            })
            cursor += timedelta(hours=1)
        running = 0.0
        for item in series:
            running += item["cost"]
            item["cost"] = round(running, 4)
        return series

    raw = tracker_db.usage_cost_by_day(days=days, firm_id=firm_id, employee_code=employee_code)
    by_day = {}
    for row in raw:
        day = _as_date(row.get("day"))
        if not day:
            continue
        by_day[day] = float(row.get("cost") or 0)

    today = datetime.now(_EASTERN).date()
    if days:
        start = (datetime.now(_EASTERN) - timedelta(days=int(days))).date()
    elif by_day:
        start = min(by_day)
    else:
        return []

    if start > today:
        start = today

    span = (today - start).days
    use_week = days is None and span > 180
    series = []

    if use_week:
        weekly = {}
        for day, cost in by_day.items():
            key = _monday(day)
            weekly[key] = weekly.get(key, 0.0) + cost
        cursor = _monday(start)
        end = _monday(today)
        while cursor <= end:
            series.append({
                "date": cursor.isoformat(),
                "label": cursor.strftime("%b ") + str(cursor.day),
                "tip": "Week of " + cursor.strftime("%b ") + str(cursor.day) + ", " + str(cursor.year),
                "cost": round(weekly.get(cursor, 0.0), 4),
            })
            cursor += timedelta(days=7)
    else:
        cursor = start
        while cursor <= today:
            series.append({
                "date": cursor.isoformat(),
                "label": cursor.strftime("%b ") + str(cursor.day),
                "tip": cursor.strftime("%b ") + str(cursor.day) + ", " + str(cursor.year),
                "cost": round(by_day.get(cursor, 0.0), 4),
            })
            cursor += timedelta(days=1)
    running = 0.0
    for item in series:
        running += item["cost"]
        item["cost"] = round(running, 4)
    return series


_FIRM_USAGE_CHART_COLORS = [
    "#ea580c",
    "#4e79a7",
    "#59a14f",
    "#e15759",
    "#76b7b2",
    "#b07aa1",
    "#edc948",
    "#9c755f",
    "#ff9da7",
    "#499894",
    "#d37295",
    "#86bcb6",
    "#f28e2b",
    "#8cd17d",
    "#b6992d",
]


def _firm_usage_chart_series(firm_id, days):
    if not tracker_db.DATABASE_URL or not firm_id:
        return None

    buckets = collections.defaultdict(lambda: collections.defaultdict(int))
    tool_totals = collections.defaultdict(int)

    if days == 1:
        for row in tracker_db.firm_usage_by_hour_tool(firm_id, days=days):
            hour = _as_et_hour(row.get("hour"))
            if not hour:
                continue
            tool = row.get("tool") or "unknown"
            n = int(row.get("calls") or 0)
            buckets[hour][tool] += n
            tool_totals[tool] += n
        if not tool_totals:
            return None
        end = datetime.now(_EASTERN).replace(minute=0, second=0, microsecond=0)
        cursor = end - timedelta(hours=23)
        points = []
        while cursor <= end:
            hour, ampm = _clock_parts(cursor)
            counts = {k: int(buckets[cursor].get(k, 0)) for k in tool_totals}
            points.append({
                "date": cursor.isoformat(),
                "label": hour + " " + ampm,
                "tip": (
                    cursor.strftime("%b ") + str(cursor.day) + ", " + str(cursor.year)
                    + ", " + hour + cursor.strftime(":%M ") + ampm + " ET"
                ),
                "counts": counts,
                "total": sum(counts.values()),
            })
            cursor += timedelta(hours=1)
    else:
        for row in tracker_db.firm_usage_by_day_tool(firm_id, days=days):
            day = _as_date(row.get("day"))
            if not day:
                continue
            tool = row.get("tool") or "unknown"
            n = int(row.get("calls") or 0)
            buckets[day][tool] += n
            tool_totals[tool] += n
        if not tool_totals:
            return None

        today = datetime.now(_EASTERN).date()
        if days:
            start = (datetime.now(_EASTERN) - timedelta(days=int(days))).date()
        else:
            start = min(buckets)
        if start > today:
            start = today

        span = (today - start).days
        use_week = days is None and span > 180
        points = []

        if use_week:
            weekly = collections.defaultdict(lambda: collections.defaultdict(int))
            for day, tools in buckets.items():
                key = _monday(day)
                for tool, n in tools.items():
                    weekly[key][tool] += n
            cursor = _monday(start)
            end = _monday(today)
            while cursor <= end:
                counts = {k: int(weekly[cursor].get(k, 0)) for k in tool_totals}
                points.append({
                    "date": cursor.isoformat(),
                    "label": cursor.strftime("%b ") + str(cursor.day),
                    "tip": "Week of " + cursor.strftime("%b ") + str(cursor.day) + ", " + str(cursor.year),
                    "counts": counts,
                    "total": sum(counts.values()),
                })
                cursor += timedelta(days=7)
        else:
            cursor = start
            while cursor <= today:
                counts = {k: int(buckets[cursor].get(k, 0)) for k in tool_totals}
                points.append({
                    "date": cursor.isoformat(),
                    "label": cursor.strftime("%b ") + str(cursor.day),
                    "tip": cursor.strftime("%b ") + str(cursor.day) + ", " + str(cursor.year),
                    "counts": counts,
                    "total": sum(counts.values()),
                })
                cursor += timedelta(days=1)

    tools_sorted = sorted(tool_totals, key=lambda k: (-tool_totals[k], k or ""))
    tools = []
    for i, key in enumerate(tools_sorted):
        tools.append({
            "key": key,
            "label": _usage_label_tool(key),
            "color": _FIRM_USAGE_CHART_COLORS[i % len(_FIRM_USAGE_CHART_COLORS)],
        })
    return {"tools": tools, "points": points}


@app.route("/admin/usage")
@admin_required
def admin_usage():
    days, days_key, firm_id, employee_code = _usage_filters_from_request()
    firms = tracker_db.list_firms() if tracker_db.DATABASE_URL else []

    overview = {
        "calls": 0, "errors": 0, "cost": 0, "pages": 0,
        "cost_openai": 0, "cost_ocr": 0, "error_rate": 0,
        "cost_delta": None, "cost_delta_pct": None,
        "cost_delta_display": None,
    }
    by_tool, by_firm, by_employee, errors = [], [], [], []

    if tracker_db.DATABASE_URL:
        kwargs = dict(days=days, firm_id=firm_id, employee_code=employee_code)
        row = tracker_db.usage_overview(**kwargs)
        if row:
            overview = dict(row)
        calls = int(overview.get("calls") or 0)
        errors_n = int(overview.get("errors") or 0)
        overview["error_rate"] = round(100.0 * errors_n / calls, 1) if calls else 0
        if days is not None:
            current_cost = float(overview.get("cost") or 0)
            prior_cost = float(overview.get("cost_prior") or 0)
            delta = current_cost - prior_cost
            overview["cost_delta"] = delta
            if prior_cost > 0:
                pct = round(100.0 * delta / prior_cost, 1)
                overview["cost_delta_pct"] = pct
                overview["cost_delta_display"] = f"{pct:+.1f}%"
            else:
                sign = "+" if delta >= 0 else "-"
                overview["cost_delta_display"] = f"{sign}${abs(delta):,.2f}"
        by_tool = tracker_db.usage_by_tool(**kwargs)
        by_firm = tracker_db.usage_by_firm(**kwargs)
        by_employee = tracker_db.usage_by_employee(**kwargs)
        errors = []
        for r in tracker_db.usage_recent_errors(**kwargs):
            item = dict(r)
            item["summary"] = _usage_error_summary(item.get("notes"))
            errors.append(item)

    cost_series = _usage_cost_series(days, firm_id, employee_code)

    scope_firm_name = None
    if firm_id:
        for f in firms:
            if str(f["id"]) == str(firm_id):
                scope_firm_name = f["name"]
                break
        if not scope_firm_name:
            scope_firm_name = "Unknown firm"

    scope_employee = None
    if employee_code:
        for row in by_employee:
            if row.get("employee_id_code") == employee_code and row.get("employee_name"):
                scope_employee = row["employee_name"]
                break
        if not scope_employee:
            scope_employee = employee_code

    return render_template(
        "admin_usage.html",
        overview=overview,
        by_tool=by_tool,
        by_firm=by_firm,
        by_employee=by_employee,
        errors=errors,
        firms=firms,
        days_key=days_key,
        selected_firm=firm_id or "",
        selected_employee=employee_code or "",
        scope_firm_name=scope_firm_name,
        scope_employee=scope_employee,
        tool_label=_usage_label_tool,
        provider_label=_usage_label_provider,
        cost_series=cost_series,
    )


@app.route("/admin/usage/csv")
@admin_required
def admin_usage_csv():
    days, days_key, firm_id, employee_code = _usage_filters_from_request()
    rows = tracker_db.usage_log_rows(
        days=days, firm_id=firm_id, employee_code=employee_code,
    ) if tracker_db.DATABASE_URL else []

    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Time (ET)", "Firm", "Employee Name", "Employee ID", "Tool", "Provider",
        "Model", "Status", "Cost USD", "Input Tokens", "Output Tokens",
        "Reasoning Tokens", "Pages", "Duration ms",
    ])
    for r in rows:
        ts = r.get("timestamp")
        writer.writerow([
            _et_time_csv(ts),
            r.get("firm_name") or "",
            r.get("employee_name") or "",
            r.get("employee_id_code") or "",
            _usage_label_tool(r.get("tool")),
            _usage_label_provider(r.get("provider")),
            r.get("model") or "",
            r.get("status") or "",
            r.get("cost_usd") if r.get("cost_usd") is not None else "",
            r.get("input_tokens") if r.get("input_tokens") is not None else "",
            r.get("output_tokens") if r.get("output_tokens") is not None else "",
            r.get("reasoning_tokens") if r.get("reasoning_tokens") is not None else "",
            r.get("pages_processed") if r.get("pages_processed") is not None else "",
            r.get("execution_ms") if r.get("execution_ms") is not None else "",
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=Usage_{time.strftime('%m-%d-%Y_%H%M%S')}.csv"
    )
    return resp


@app.route("/admin/firms/new", methods=["GET", "POST"])
@admin_required
def admin_firm_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        slug = request.form.get("slug", "").strip()
        access_code = request.form.get("access_code", "").strip()
        tracker_code = request.form.get("tracker_access_code", "").strip()
        require_employee_login = bool(request.form.get("require_employee_login"))
        employee_login_hint = (request.form.get("employee_login_hint") or "").strip()
        usage_dash_on = bool(request.form.get("usage_dashboard_enabled"))
        usage_dash_show_costs = bool(request.form.get("usage_dashboard_show_costs"))
        usage_dash_pw = (request.form.get("usage_dashboard_password") or "").strip()
        feedback_widget_on = bool(request.form.get("feedback_widget_enabled"))

        if not all([name, slug, access_code]):
            return render_template("admin_firm_edit.html", firm=None,
                                   error="Firm name, slug, and tool access code are required.")

        try:
            config = _parse_config_from_form(request.form)
        except ConfigParseError as e:
            return render_template("admin_firm_edit.html", firm=None, error=str(e))

        errors = _validate_config(config)
        stub = {"name": name, "slug": slug, "config": config,
                "require_employee_login": require_employee_login,
                "employee_login_hint": employee_login_hint,
                "usage_dashboard_enabled": usage_dash_on,
                "usage_dashboard_show_costs": usage_dash_show_costs,
                "usage_dashboard_password_plain": usage_dash_pw,
                "feedback_widget_enabled": feedback_widget_on,
                "tracker_access_code_plain": tracker_code}
        if config.get("tools_enabled", {}).get("tracker") and not tracker_code:
            errors.append("A tracker access code is required when Client Progress Tracker is enabled.")
        if errors:
            return render_template("admin_firm_edit.html", firm=stub,
                                   error=" | ".join(errors))
        if usage_dash_on and not usage_dash_pw:
            return render_template("admin_firm_edit.html", firm=stub,
                                   error="A dashboard password is required when the usage dashboard is enabled.")

        try:
            tracker_db.create_firm(name, slug, access_code, tracker_code or "", config,
                                   require_employee_login=require_employee_login,
                                   employee_login_hint=employee_login_hint,
                                   usage_dashboard_enabled=usage_dash_on,
                                   usage_dashboard_show_costs=usage_dash_show_costs,
                                   usage_dashboard_password=usage_dash_pw,
                                   feedback_widget_enabled=feedback_widget_on)
        except Exception as e:
            if "unique" in str(e).lower():
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
        require_employee_login = bool(request.form.get("require_employee_login"))
        employee_login_hint = (request.form.get("employee_login_hint") or "").strip()
        usage_dash_on = bool(request.form.get("usage_dashboard_enabled"))
        usage_dash_show_costs = bool(request.form.get("usage_dashboard_show_costs"))
        usage_dash_pw = (request.form.get("usage_dashboard_password") or "").strip() or None
        feedback_widget_on = bool(request.form.get("feedback_widget_enabled"))

        if not name or not slug:
            return render_template("admin_firm_edit.html", firm=firm,
                                   error="Firm name and slug are required.")

        try:
            config = _parse_config_from_form(request.form)
        except ConfigParseError as e:
            return render_template("admin_firm_edit.html", firm=firm, error=str(e))

        errors = _validate_config(config)
        firm["config"] = config
        firm["require_employee_login"] = require_employee_login
        firm["employee_login_hint"] = employee_login_hint
        firm["usage_dashboard_enabled"] = usage_dash_on
        firm["usage_dashboard_show_costs"] = usage_dash_show_costs
        firm["feedback_widget_enabled"] = feedback_widget_on
        if config.get("tools_enabled", {}).get("tracker") and not tracker_code and not firm.get("tracker_access_code_plain"):
            errors.append("A tracker access code is required when Client Progress Tracker is enabled.")
        if errors:
            return render_template("admin_firm_edit.html", firm=firm,
                                   error=" | ".join(errors))
        if usage_dash_on and not usage_dash_pw and not firm.get("usage_dashboard_password_plain"):
            return render_template("admin_firm_edit.html", firm=firm,
                                   error="A dashboard password is required when the usage dashboard is enabled.")

        try:
            tracker_db.update_firm(firm_id, name=name, slug=slug,
                                   access_code=access_code,
                                   tracker_access_code=tracker_code,
                                   config=config,
                                   require_employee_login=require_employee_login,
                                   employee_login_hint=employee_login_hint,
                                   usage_dashboard_enabled=usage_dash_on,
                                   usage_dashboard_show_costs=usage_dash_show_costs,
                                   usage_dashboard_password=usage_dash_pw,
                                   feedback_widget_enabled=feedback_widget_on)
        except Exception as e:
            if "unique" in str(e).lower():
                return render_template("admin_firm_edit.html", firm=firm,
                                       error="A firm with that slug already exists.")
            raise

        _invalidate_firm_cache(firm_id)

        return redirect(url_for("admin_firms"))

    return render_template("admin_firm_edit.html", firm=firm)


@app.route("/admin/firms/<firm_id>/delete", methods=["POST"])
@admin_required
def admin_firm_delete(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    expected = f"delete firm {firm['slug']}"
    confirmation = request.form.get("confirmation", "").strip()
    if confirmation != expected:
        return render_template("admin_firm_edit.html", firm=firm,
                               error="Deletion confirmation did not match. Firm was not deleted.")
    tracker_db.delete_firm(firm_id)
    _invalidate_firm_cache(firm_id)
    return redirect(url_for("admin_firms"))


@app.route("/admin/firms/<firm_id>/specimens")
@admin_required
def admin_specimens(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    specimens = tracker_db.list_specimen_documents(firm_id)
    return render_template("admin_specimens.html", firm=firm, specimens=specimens)


@app.route("/admin/firms/<firm_id>/specimens/upload", methods=["POST"])
@admin_required
def admin_specimen_upload(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    f = request.files.get("docx")
    if not name or not f or not f.filename.lower().endswith(".docx"):
        return redirect(url_for("admin_specimens", firm_id=firm_id))
    tracker_db.create_specimen_document(firm_id, name, f.read(), description=description)
    return redirect(url_for("admin_specimens", firm_id=firm_id))


@app.route("/admin/firms/<firm_id>/specimens/<doc_id>/edit", methods=["POST"])
@admin_required
def admin_specimen_edit(firm_id, doc_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if name:
        tracker_db.update_specimen_document(doc_id, name, description=description, firm_id=firm_id)
    return redirect(url_for("admin_specimens", firm_id=firm_id))


@app.route("/admin/firms/<firm_id>/specimens/<doc_id>/download")
@admin_required
def admin_specimen_download(firm_id, doc_id):
    doc = tracker_db.get_specimen_document(doc_id, firm_id=firm_id)
    if not doc:
        return redirect(url_for("admin_specimens", firm_id=firm_id))
    return Response(
        doc["docx_data"],
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename={doc['name']}.docx"},
    )


@app.route("/admin/firms/<firm_id>/specimens/<doc_id>/delete", methods=["POST"])
@admin_required
def admin_specimen_delete(firm_id, doc_id):
    tracker_db.delete_specimen_document(doc_id)
    return redirect(url_for("admin_specimens", firm_id=firm_id))


def _parse_employee_csv(file_storage):
    import csv as csv_mod

    raw = file_storage.read()
    if len(raw) > 1_000_000:
        raise ValueError("CSV file is too large.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV must be UTF-8 encoded.")

    reader = csv_mod.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV is missing a header row.")
    headers = [h.strip() for h in reader.fieldnames]
    if set(headers) != {"id", "name"}:
        raise ValueError('CSV must have exactly two columns named "id" and "name".')

    rows = []
    seen = set()
    for i, row in enumerate(reader, start=2):
        emp_id = (row.get("id") or "").strip()
        name = (row.get("name") or "").strip()
        if not emp_id and not name:
            continue
        if not emp_id or not name:
            raise ValueError(f"Row {i}: both id and name are required.")
        if emp_id in seen:
            raise ValueError(f'Row {i}: duplicate id "{emp_id}".')
        seen.add(emp_id)
        rows.append((emp_id, name))
        if len(rows) > 5000:
            raise ValueError("CSV cannot contain more than 5000 employees.")
    if not rows:
        raise ValueError("CSV contains no employee rows.")
    return rows


def _admin_employees_page(firm, error=None, success=None, add_mode="add"):
    employees = tracker_db.list_employees(firm["id"])
    return render_template(
        "admin_employees.html",
        firm=firm,
        employees=employees,
        error=error,
        success=success,
        add_mode=add_mode,
    )


@app.route("/admin/firms/<firm_id>/employees")
@admin_required
def admin_employees(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    success = None
    if request.args.get("added"):
        success = "Employee added."
    imported = request.args.get("imported")
    if imported:
        try:
            n = int(imported)
            success = f"Imported {n} employee{'s' if n != 1 else ''}."
        except ValueError:
            success = "Employees imported."
    return _admin_employees_page(firm, success=success)


@app.route("/admin/firms/<firm_id>/employees/add", methods=["POST"])
@admin_required
def admin_employee_add(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    emp_id = (request.form.get("employee_id") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not emp_id or not name:
        return _admin_employees_page(firm, error="Employee ID and name are required.")
    try:
        tracker_db.create_employee(firm_id, emp_id, name)
    except Exception as e:
        if "unique" in str(e).lower():
            return _admin_employees_page(firm, error="An employee with that ID already exists.")
        raise
    return redirect(url_for("admin_employees", firm_id=firm_id, added="1"))


@app.route("/admin/firms/<firm_id>/employees/upload", methods=["POST"])
@admin_required
def admin_employee_upload(firm_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    f = request.files.get("csv")
    if not f or not f.filename:
        return _admin_employees_page(firm, error="Please choose a CSV file.", add_mode="csv")
    try:
        rows = _parse_employee_csv(f)
        count = tracker_db.upsert_employees(firm_id, rows)
    except ValueError as e:
        return _admin_employees_page(firm, error=str(e), add_mode="csv")
    return redirect(url_for("admin_employees", firm_id=firm_id, imported=count))


@app.route("/admin/firms/<firm_id>/employees/<employee_id>/delete", methods=["POST"])
@admin_required
def admin_employee_delete(firm_id, employee_id):
    firm = tracker_db.get_firm(firm_id)
    if not firm:
        return redirect(url_for("admin_firms"))
    tracker_db.delete_employee(employee_id, firm_id=firm_id)
    return redirect(url_for("admin_employees", firm_id=firm_id))


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
        "doc_differences": bool(form.get("tool_doc_differences")),
        "tracker": bool(form.get("tool_tracker")),
        "estate_tax_calc": bool(form.get("tool_estate_tax_calc")),
    }

    for key, tool in (
        ("ep_schema", "drafting_notes"),
        ("prospect_schema", "prospect_summarizer"),
        ("tracker_default_steps", "tracker"),
    ):
        raw = form.get(key, "").strip()
        empty = [] if key == "tracker_default_steps" else {}
        if not raw:
            config[key] = empty
            continue
        try:
            config[key] = json.loads(raw)
        except json.JSONDecodeError as e:
            if config["tools_enabled"].get(tool):
                raise ConfigParseError(f"Invalid JSON in {key}: {e}")
            config[key] = empty

    return config


def _validate_config(config):
    errors = []
    tools = config.get("tools_enabled") or {}
    if (tools.get("drafting_notes") or tools.get("prospect_summarizer")) and not config.get("firm_context"):
        errors.append("Firm context is required when Drafting Notes or Prospect Summarizer is enabled")
    if tools.get("drafting_notes"):
        ep = config.get("ep_schema")
        if not ep or not isinstance(ep, dict) or not ep.get("sections"):
            errors.append("EP schema must be valid JSON with a 'sections' array")
    if tools.get("prospect_summarizer"):
        ps = config.get("prospect_schema")
        if not ps or not isinstance(ps, dict) or not ps.get("sections"):
            errors.append("Prospect schema must be valid JSON with a 'sections' array")
    if tools.get("tracker"):
        ts = config.get("tracker_default_steps")
        if not ts or not isinstance(ts, list):
            errors.append("Tracker default steps must be a JSON array")
    if tools.get("doc_separator") and not config.get("doc_filename_format"):
        errors.append("Document filename format is required when Document Separator is enabled")
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


def _notify_tool_error(tool_name, error, firm_id=None, firm_name=None, firm_slug=None,
                       details=None):
    """Send alert for tool errors that don't trigger the 500 error handler."""
    if not RESEND_API_KEY or not _can_send_alert():
        return
    if firm_name is None:
        firm_name = session.get("firm_name", "N/A") if has_request_context() else "N/A"
    if firm_slug is None:
        firm_slug = session.get("firm_slug", "N/A") if has_request_context() else "N/A"

    tb = traceback.format_exc()
    req_url = f"{request.method} {request.url}" if has_request_context() else "background job"

    def _e(value):
        return html.escape(str(value) if value is not None else "")

    details_html = ""
    if details:
        rows = []
        for key, value in details.items():
            if value is None or value == "":
                continue
            if key in ("response_start", "response_end"):
                rows.append(
                    f"<p><strong>{_e(key)}:</strong></p>"
                    f"<pre style=\"font-size:12px;white-space:pre-wrap;\">{_e(value)}</pre>"
                )
            else:
                rows.append(f"<p><strong>{_e(key)}:</strong> {_e(value)}</p>")
        if rows:
            details_html = "<h3>Model diagnostics</h3>" + "".join(rows) + "<hr>"

    body = (
        f"<h2>Tool Error: {_e(tool_name)}</h2>"
        f"<p><strong>Time:</strong> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</p>"
        f"<p><strong>URL:</strong> {_e(req_url)}</p>"
        f"<p><strong>Firm:</strong> {_e(firm_name)} ({_e(firm_slug)})</p>"
        f"<p><strong>Error:</strong> {_e(error)}</p>"
        f"{details_html}"
        f"<pre style=\"font-size:12px;white-space:pre-wrap;\">{_e(tb)}</pre>"
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
