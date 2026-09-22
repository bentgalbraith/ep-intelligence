"""Canonical `log_ai_call(tool=...)` keys and how they appear on dashboards.

When you add a new tool key, do this here first — not in templates or ad-hoc
SQL. Admin usage stays uncollapsed (every log row, including steps).

1. TOOL_LABELS  — human name for admin and for the parent product on the firm
   dashboard.
2. STEP_OF      — internal step of an existing product (OCR, extra API hops).
   Firm dashboard: hide successful steps from uses, by-tool, charts, and the
   per-use list. Failed steps count as a use of the parent (no parent row is
   written when the step fails). Costs still count in Total / Model / OCR via
   `provider`, not via this map.
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
    "doc_differences": "Identify Document Differences",
    "estate_tax_calc": "Estate Tax Calculator",
    "community_property_trust_calc": "Community Property Trust Calculator",
    "compare_diagram_drafts": "Compare EP Diagram vs. Drafts",
    "compare_diagram_drafts_batch": "Compare EP Diagram vs. Drafts (batch)",
    "actionstep_schedule": "Visualize Actionstep Schedule",
    "tracker": "Client Progress Tracker",
}

# log key -> parent product key
STEP_OF = {
    "doc_separator_ocr": "doc_separator",
    "prospect_summarizer_ocr": "prospect_summarizer",
    "compare_diagram_drafts_batch": "compare_diagram_drafts",
}

ALIAS_OF = {
    "doc_separator_redo": "doc_separator",
}


def display_label(tool):
    return TOOL_LABELS.get(tool, tool or "—")


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def firm_product_tool_sql(column="l.tool"):
    """SQL expr that maps alias and step keys to the parent product."""
    mapping = {**STEP_OF, **ALIAS_OF}
    if not mapping:
        return column
    whens = " ".join(
        f"WHEN {column} = {_sql_literal(src)} THEN {_sql_literal(dst)}"
        for src, dst in sorted(mapping.items())
    )
    return f"CASE {whens} ELSE {column} END"


def firm_use_row_sql(column="l.tool", status_column="l.status"):
    """TRUE for rows that count as a firm-facing use.

    Successful internal steps are hidden. Failed steps count as the parent.
    TRUE when the map is empty.
    """
    keys = sorted(STEP_OF)
    if not keys:
        return "TRUE"
    joined = ", ".join(_sql_literal(k) for k in keys)
    return f"({column} NOT IN ({joined}) OR {status_column} = 'error')"
