"""Centralized AI usage logger — writes every AI call to the ai_usage_log table."""

import contextvars
import logging
import os
import traceback
from contextlib import contextmanager

import psycopg2

log = logging.getLogger("ai_logger")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

PROJECT = "ep_intelligence"

MODEL_PRICING = {
    "gpt-5.6-sol": {
        "input": 5.00e-6,
        "output": 30.00e-6,
        "reasoning": 30.00e-6,
    },
    "gpt-5.6-terra": {
        "input": 2.50e-6,
        "output": 15.00e-6,
        "reasoning": 15.00e-6,
    },
    "gpt-5.6-luna": {
        "input": 1.00e-6,
        "output": 6.00e-6,
        "reasoning": 6.00e-6,
    },
}

DOCUMENTAI_COST_PER_PAGE = 0.0015


def _compute_cost(provider, model, input_tokens, output_tokens, reasoning_tokens, pages_processed):
    if provider == "google_documentai" and pages_processed:
        return round(pages_processed * DOCUMENTAI_COST_PER_PAGE, 6)

    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return None

    cost = 0.0
    if input_tokens:
        cost += input_tokens * pricing.get("input", 0)
    if output_tokens:
        cost += output_tokens * pricing.get("output", 0)
    if reasoning_tokens:
        cost += reasoning_tokens * pricing.get("reasoning", pricing.get("output", 0))
    return round(cost, 6)


_log_ctx = contextvars.ContextVar("ai_log_ctx", default=None)


@contextmanager
def log_context(**kwargs):
    """Attach employee attribution for background jobs that have no Flask session."""
    cleaned = {k: v for k, v in kwargs.items() if v}
    token = _log_ctx.set(cleaned)
    try:
        yield
    finally:
        _log_ctx.reset(token)


def _resolve_employee(employee_id, employee_id_code, employee_name):
    ctx = _log_ctx.get() or {}
    employee_id = employee_id or ctx.get("employee_id")
    employee_id_code = employee_id_code or ctx.get("employee_id_code")
    employee_name = employee_name or ctx.get("employee_name")
    if not employee_id:
        try:
            from flask import has_request_context, session
            if has_request_context():
                employee_id = session.get("employee_id")
                employee_id_code = employee_id_code or session.get("employee_code")
                employee_name = employee_name or session.get("employee_name")
        except Exception:
            pass
    return employee_id, employee_id_code, employee_name


_INSERT_SQL = """INSERT INTO ai_usage_log
                   (provider, model, project, tool, status,
                    input_tokens, output_tokens, reasoning_tokens,
                    pages_processed, cost_usd, execution_ms, notes, firm_id,
                    employee_id, employee_id_code, employee_name)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def log_ai_call(
    *,
    provider,
    tool,
    status,
    model=None,
    input_tokens=None,
    output_tokens=None,
    reasoning_tokens=None,
    pages_processed=None,
    execution_ms=None,
    notes=None,
    firm_id=None,
    employee_id=None,
    employee_id_code=None,
    employee_name=None,
):
    cost = _compute_cost(provider, model, input_tokens, output_tokens, reasoning_tokens, pages_processed)
    employee_id, employee_id_code, employee_name = _resolve_employee(
        employee_id, employee_id_code, employee_name,
    )

    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — skipping AI usage log")
        return

    values = (
        provider, model, PROJECT, tool, status,
        input_tokens, output_tokens, reasoning_tokens,
        pages_processed, cost, execution_ms, notes,
        str(firm_id) if firm_id else None,
        str(employee_id) if employee_id else None,
        employee_id_code or None,
        employee_name or None,
    )
    values_no_emp_id = values[:-3] + (None, employee_id_code or None, employee_name or None)

    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(_INSERT_SQL, values)
                    conn.commit()
                except psycopg2.Error:
                    conn.rollback()
                    if employee_id:
                        cur.execute(_INSERT_SQL, values_no_emp_id)
                        conn.commit()
                    else:
                        raise
        finally:
            conn.close()
    except Exception:
        log.error("Failed to write AI usage log: %s", traceback.format_exc())


def extract_xai_usage(response):
    """Pull token counts from an OpenAI response object."""
    usage = response.usage
    if not usage:
        return {}

    result = {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
    }

    details = getattr(usage, "completion_tokens_details", None)
    if details:
        result["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)

    return result


def completion_details(response, raw=""):
    """Diagnostics for error emails: finish_reason, tokens, response snippet."""
    choice = response.choices[0] if response and getattr(response, "choices", None) else None
    finish = getattr(choice, "finish_reason", None) if choice else None
    usage = extract_xai_usage(response) if response else {}
    details = {
        "finish_reason": finish or "(none)",
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "response_chars": len(raw or ""),
    }
    if finish == "length":
        details["likely_cause"] = "Output truncated (hit token limit)"
    elif raw:
        details["likely_cause"] = "Model returned invalid JSON"
    if raw:
        details["response_start"] = raw[:500]
        if len(raw) > 500:
            details["response_end"] = raw[-300:]
    return details
