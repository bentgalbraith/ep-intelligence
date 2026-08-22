"""Canonical `log_ai_call(tool=...)` keys and how they appear on dashboards.

When you add a new tool key, do this here first — not in templates or ad-hoc
SQL. Admin usage stays uncollapsed (every log row, including steps).

1. TOOL_LABELS  — human name for admin and for the parent product on the firm
   dashboard.
2. STEP_OF      — internal step of an existing product (OCR, extra API hops).
   Firm dashboard: hide from uses, by-tool, charts, and the per-use list.
   Costs still count in Total / Model / OCR via `provider`, not via this map.
3. ALIAS_OF     — same product, different log key (redo, retry). Firm dashboard:
   count as a use of the parent; do not show a separate tool row.
"""

TOOL_LABELS = {
    "ep_extract": "Drafting Notes",
    "doc_separator": "Document Separator",
    "doc_separator_ocr": "Document Separator (OCR)",
    "doc_separator_redo": "Document Separator (Redo)",
    "prospect_summarizer": "Prospect Summarizer",
    "prospect_summarizer_ocr": "Prospect Summarizer (OCR)",
}

# log key -> parent product key
STEP_OF = {
    "doc_separator_ocr": "doc_separator",
    "prospect_summarizer_ocr": "prospect_summarizer",
}

ALIAS_OF = {
    "doc_separator_redo": "doc_separator",
}


def display_label(tool):
    return TOOL_LABELS.get(tool, tool or "—")


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def firm_product_tool_sql(column="l.tool"):
    """SQL expr that maps alias keys to the parent product. Steps are unchanged."""
    if not ALIAS_OF:
        return column
    whens = " ".join(
        f"WHEN {column} = {_sql_literal(src)} THEN {_sql_literal(dst)}"
        for src, dst in sorted(ALIAS_OF.items())
    )
    return f"CASE {whens} ELSE {column} END"


def firm_step_not_in_sql(column="l.tool"):
    """SQL excluding internal steps. TRUE when the map is empty."""
    keys = sorted(STEP_OF)
    if not keys:
        return "TRUE"
    joined = ", ".join(_sql_literal(k) for k in keys)
    return f"{column} NOT IN ({joined})"
