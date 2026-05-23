"""Centralized AI usage logger — writes every AI call to the ai_usage_log table."""

import logging
import os
import traceback

import psycopg2

log = logging.getLogger("ai_logger")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

PROJECT = "ep_intelligence"

MODEL_PRICING = {
    "grok-4-1-fast-reasoning": {
        "input": 0.20e-6,
        "output": 0.50e-6,
        "reasoning": 0.50e-6,
    },
    "grok-4-1-fast-non-reasoning": {
        "input": 0.20e-6,
        "output": 0.50e-6,
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
):
    cost = _compute_cost(provider, model, input_tokens, output_tokens, reasoning_tokens, pages_processed)

    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — skipping AI usage log")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO ai_usage_log
                       (provider, model, project, tool, status,
                        input_tokens, output_tokens, reasoning_tokens,
                        pages_processed, cost_usd, execution_ms, notes, firm_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        provider, model, PROJECT, tool, status,
                        input_tokens, output_tokens, reasoning_tokens,
                        pages_processed, cost, execution_ms, notes,
                        str(firm_id) if firm_id else None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.error("Failed to write AI usage log: %s", traceback.format_exc())


def extract_xai_usage(response):
    """Pull token counts from an xAI/OpenAI response object."""
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
