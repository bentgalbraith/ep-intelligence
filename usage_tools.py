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
4. FEED_OWN_USE — a step that is its own click (not a per-page hop). The admin
   feed shows it as its own use. Leave internal hops out of this set.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

TOOL_LABELS = {
    "ep_extract": "Drafting Notes",
    "doc_separator": "Document Separator",
    "doc_separator_ocr": "Document Separator (OCR)",
    "doc_separator_redo": "Document Separator (Redo)",
    "doc_separator_qprt": "Document Separator (QPRT term)",
    "prospect_summarizer": "Prospect Summarizer",
    "prospect_summarizer_ocr": "Prospect Summarizer (OCR)",
    "doc_differences": "Identify Document Differences",
    "estate_tax_calc": "Estate Tax Calculator",
    "community_property_trust_calc": "Community Property Trust Calculator",
    "compare_diagram_drafts": "Compare EP Diagram vs. Drafts",
    "compare_diagram_drafts_batch": "Compare EP Diagram vs. Drafts (batch)",
    "actionstep_schedule": "Visualize Actionstep Schedule",
    "validate_signatures": "Name Files & Validate Signatures",
    "validate_signatures_scan": "Name Files & Validate Signatures (scan)",
    "validate_signatures_verify": "Name Files & Validate Signatures (verify)",
    "validate_signatures_crop": "Name Files & Validate Signatures (screenshot)",
    "validate_signatures_name": "Name Files & Validate Signatures (name)",
    "validate_signatures_name_verify": "Name Files & Validate Signatures (name check)",
    "client_doc_summary": "Summarize Documents for Clients",
    "client_doc_summary_ocr": "Summarize Documents for Clients (OCR)",
    "client_doc_summary_part": "Summarize Documents for Clients (part)",
    "client_doc_summary_redo": "Summarize Documents for Clients (redo)",
    "tracker": "Client Progress Tracker",
}

# log key -> parent product key
STEP_OF = {
    "doc_separator_ocr": "doc_separator",
    "doc_separator_qprt": "doc_separator",
    "prospect_summarizer_ocr": "prospect_summarizer",
    "compare_diagram_drafts_batch": "compare_diagram_drafts",
    "client_doc_summary_ocr": "client_doc_summary",
    "client_doc_summary_part": "client_doc_summary",
    "validate_signatures_scan": "validate_signatures",
    "validate_signatures_verify": "validate_signatures",
    "validate_signatures_crop": "validate_signatures",
    "validate_signatures_name": "validate_signatures",
    "validate_signatures_name_verify": "validate_signatures",
}

ALIAS_OF = {
    "doc_separator_redo": "doc_separator",
    "client_doc_summary_redo": "client_doc_summary",
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


# A step that is its own click, not a hop inside a run. The firm dashboard
# still counts these with the parent. The admin feed shows them on their own.
FEED_OWN_USE = frozenset({
    "doc_separator_qprt",
})

# Silence longer than this, for the same firm, person, and product, is a new use.
FEED_GAP = timedelta(minutes=15)


def product_key(tool):
    if tool in ALIAS_OF:
        return ALIAS_OF[tool]
    if tool in STEP_OF:
        return STEP_OF[tool]
    return tool or ""


def feed_anchor(tool):
    """A row that is one person using a tool, not a hop inside that use."""
    return tool not in STEP_OF or tool in FEED_OWN_USE


def feed_row_sql(column="l.tool", status_column="l.status"):
    """Rows worth reading for the admin feed.

    Successful internal hops (one model call per page, OCR, and so on) are
    left out. A failed hop stays, so a run that died before its parent row
    was written still shows up. TRUE when nothing is hidden.
    """
    hidden = sorted(set(STEP_OF) - set(FEED_OWN_USE))
    if not hidden:
        return "TRUE"
    joined = ", ".join(_sql_literal(k) for k in hidden)
    return f"({column} NOT IN ({joined}) OR {status_column} = 'error')"


def feed_step_cost_sql(column="l.tool", status_column="l.status"):
    """Successful internal hops. Their cost belongs to a use, but they are not one."""
    hidden = sorted(set(STEP_OF) - set(FEED_OWN_USE))
    if not hidden:
        return "FALSE"
    joined = ", ".join(_sql_literal(k) for k in hidden)
    return f"({column} IN ({joined}) AND {status_column} = 'success')"


def format_feed_duration(seconds):
    """A recorded runtime. ``None`` means the row did not store one."""
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    if seconds < 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hr")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} sec")
    return " ".join(parts)


def format_feed_cost(amount):
    """A dollar amount for one use. ``None`` means nothing was recorded."""
    if amount is None:
        return ""
    amount = Decimal(amount)
    if amount == 0:
        return "$0.00"
    cents = amount.quantize(Decimal("0.01"))
    if cents == 0:
        return "< $0.01"
    return f"${cents:,.2f}"


def _row_cost(row):
    value = row.get("cost_usd")
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _feed_cost(rows):
    total = None
    for row in rows:
        amount = _row_cost(row)
        if amount is None:
            continue
        total = amount if total is None else total + amount
    return format_feed_cost(total)


def _feed_label(rows):
    anchor = next((row for row in reversed(rows) if feed_anchor(row.get("tool"))), None)
    if anchor is not None:
        return display_label(anchor.get("tool"))
    tools = {row.get("tool") for row in rows}
    if len(tools) == 1:
        return display_label(next(iter(tools)))
    return display_label(product_key(next(iter(rows)).get("tool")))


def _feed_status(rows):
    anchor = next((row for row in reversed(rows) if feed_anchor(row.get("tool"))), None)
    if anchor is not None:
        return anchor.get("status") or "success"
    if any(row.get("status") == "error" for row in rows):
        return "error"
    return "success"


def _feed_item(rows):
    rows = sorted(rows, key=lambda row: row["timestamp"])
    latest = rows[-1]
    person = next((row for row in reversed(rows) if feed_anchor(row.get("tool"))), latest)
    started, ended = rows[0]["timestamp"], latest["timestamp"]
    ran = _execution_seconds(person) if feed_anchor(person.get("tool")) else None
    return {
        "timestamp": ended,
        "started": started,
        "duration": format_feed_duration(ran),
        "cost": _feed_cost(rows),
        "firm_name": person.get("firm_name") or "",
        "employee_name": person.get("employee_name") or "",
        "employee_id_code": person.get("employee_id_code") or "",
        "tool": _feed_label(rows),
        "status": _feed_status(rows),
    }


def _execution_seconds(row):
    """How long the logged use ran, or None when the row did not record it."""
    value = row.get("execution_ms")
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value / 1000.0


def _covers_earlier_hops(anchor, earliest):
    """True when a finishing row timed a run that reaches back to `earliest`.

    Successful hops are left out of the feed query, so a failed hop at the
    start of a long run can sit well before the row written at the end.
    """
    ran = _execution_seconds(anchor)
    if ran is None or anchor["timestamp"] < earliest["timestamp"]:
        return False
    delta = (anchor["timestamp"] - earliest["timestamp"]).total_seconds()
    return delta <= ran + 120


def _gap_chains(group, gap):
    chains = [[group[0]]]
    limit = gap.total_seconds()
    for row in group[1:]:
        pause = (row["timestamp"] - chains[-1][-1]["timestamp"]).total_seconds()
        if pause > limit:
            chains.append([row])
        else:
            chains[-1].append(row)
    return chains


def _join_long_runs(chains):
    """Pull an unfinished stretch into the run whose timer covers it."""
    joined = list(chains)
    index = 1
    while index < len(joined):
        previous = joined[index - 1]
        anchor = None
        if not any(feed_anchor(row.get("tool")) for row in previous):
            anchor = next(
                (row for row in joined[index] if feed_anchor(row.get("tool"))),
                None,
            )
        if anchor is not None and _covers_earlier_hops(anchor, previous[0]):
            joined[index - 1] = previous + joined[index]
            del joined[index]
            if index > 1:
                index -= 1
            continue
        index += 1
    return joined


def _hop_in_run(step, anchor, gap):
    """True when this hop is part of the run `anchor` finished.

    A recorded duration wins. A hop from a few minutes earlier is an earlier
    run, not part of a short one. With no duration, only a hop still inside
    the gap belongs to that row.
    """
    if anchor["timestamp"] < step["timestamp"]:
        return False
    if _execution_seconds(anchor) is not None:
        return _covers_earlier_hops(anchor, step)
    delta = (anchor["timestamp"] - step["timestamp"]).total_seconds()
    return delta <= gap.total_seconds()


def _cost_indexes(preceding, anchor, gap):
    """Hops that belong to this run, plus page calls that lead up to them.

    A recorded duration is only the last model call for some tools. Earlier
    calls in that same stretch still belong to it. A call from a different
    run, with a real hop in between that is not part of this one, does not.
    """
    owned = {i for i, row in enumerate(preceding) if _hop_in_run(row, anchor, gap)}
    changed = True
    while changed:
        changed = False
        for i, row in enumerate(preceding):
            if i in owned or not row.get("cost_only"):
                continue
            if (i > 0 and (i - 1) in owned) or (i + 1 < len(preceding) and (i + 1) in owned):
                owned.add(i)
                changed = True
    return owned


def _loose_items(rows, gap):
    items = []
    for stretch in _gap_chains(rows, gap) if rows else []:
        if any(not row.get("cost_only") for row in stretch):
            items.append(_feed_item(stretch))
    return items


def _chain_items(chain, gap):
    anchor_at = [index for index, row in enumerate(chain) if feed_anchor(row.get("tool"))]
    if not anchor_at:
        return _loose_items(chain, gap)
    items = []
    cursor = 0
    for index in anchor_at:
        anchor = chain[index]
        preceding = chain[cursor:index]
        owned_idx = _cost_indexes(preceding, anchor, gap)
        owned = [row for i, row in enumerate(preceding) if i in owned_idx]
        loose = [row for i, row in enumerate(preceding) if i not in owned_idx]
        items.extend(_loose_items(loose, gap))
        items.append(_feed_item([*owned, anchor]))
        cursor = index + 1
    if cursor < len(chain):
        items.extend(_loose_items(chain[cursor:], gap))
    return items


def feed_items(rows, extra_costs=None, gap=FEED_GAP):
    """One item per time someone used a tool.

    A parent row, a redo, or a follow-up click is one use. Hops for the same
    firm, person, and product belong to the use that finished them. A gap
    starts another use. Hops with no finishing row are one item per stretch.
    """
    grouped = {}
    rows = list(rows)
    for row in extra_costs or []:
        tagged = dict(row)
        tagged["cost_only"] = True
        rows.append(tagged)
    for row in rows:
        if not row.get("timestamp"):
            continue
        key = (
            str(row.get("firm_id") or ""),
            (row.get("employee_id_code") or "").strip(),
            product_key(row.get("tool")),
        )
        grouped.setdefault(key, []).append(row)

    items = []
    for group in grouped.values():
        group.sort(key=lambda row: row["timestamp"])
        for chain in _join_long_runs(_gap_chains(group, gap)):
            items.extend(_chain_items(chain, gap))

    items.sort(key=lambda item: item["timestamp"], reverse=True)
    return items

