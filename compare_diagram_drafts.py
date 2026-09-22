"""Compare an estate-planning PowerPoint diagram against Word drafts."""

import io
import json
import logging
import os
import re
import time
import traceback
import zipfile

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

log = logging.getLogger("compare_diagram_drafts")

MAX_WORD_DOCS = 20
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_CHARS = 250_000
MAX_ZIP_MEMBERS = 4000
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
MAX_COMPARE_PASSES = 40
MIN_CHUNK_CHARS = 4_000
CHUNK_OVERLAP = 1_200
_PART_LABEL_PAD = 120

TOOL_KEY = "compare_diagram_drafts"
BATCH_TOOL_KEY = "compare_diagram_drafts_batch"

_SHARED_RULES = """\
Treat the PowerPoint as the intended estate plan blueprint. A conflict \
between the diagram and the drafts is a drafting issue unless the drafts \
expressly state a different client instruction. Be aggressive about omitted \
beneficiaries, fiduciaries, successor fiduciaries, powers of appointment, \
charitable gifts, and distribution provisions. Flag name spelling \
differences. Do not invent facts. Do not give legal advice. Use the generic \
labels in the checklist — not jurisdiction-specific instrument abbreviations.
"""

COMPARE_PROMPT = """\
You are a legal assistant specializing in estate planning.{firm_context}

Compare the draft Word documents to the estate-planning diagram.
""" + _SHARED_RULES + """
Review every checklist item. Return ONLY valid JSON (no markdown) with:
{{
  "verdict": "congruent" | "mostly_congruent" | "not_congruent" | "incomplete",
  "summary": "one or two sentences",
  "sections": {{
    "<section_id>": {{
      "items": [
        {{
          "id": "<item_id>",
          "status": "match" | "intentional" | "conflict" | "omission" | "not_found" | "unclear",
          "summary": "short specific note",
          "diagram": "what the diagram shows, or empty",
          "documents": "what the drafts do, or empty",
          "location": "filename and article/section when known, or empty",
          "fix": "concrete change, or empty"
        }}
      ]
    }}
  }}
}}

Statuses: match = diagram and drafts agree; intentional = they differ but it \
looks like a drafting decision (including extras in the drafts that the \
diagram does not show); conflict = incorrect or conflicting; omission = on \
the diagram but missing from the drafts; not_found = neither source \
addresses this item; unclear = the extract is too thin to decide.

Verdict: congruent if the drafts implement the diagram; mostly_congruent \
for minor revisions; not_congruent for material conflicts or omissions; \
incomplete if the extract is too thin. not_found and unclear must not drive \
the verdict. Every item id must appear exactly once. not_found is useful \
when the plan simply does not use that device.

Checklist:
{checklist}
"""

BATCH_PROMPT = """\
You are a legal assistant specializing in estate planning.{firm_context}

You are reviewing only a portion of the drafts against the complete diagram. \
Other portions are reviewed separately.
""" + _SHARED_RULES + """
Record only items this portion can support. Do not mark an item not_found \
just because it is absent from this slice — omit it. Do not write a verdict.

Return ONLY valid JSON (no markdown):
{{
  "findings": [
    {{
      "id": "<item_id>",
      "status": "match" | "intentional" | "conflict" | "omission" | "unclear",
      "summary": "short specific note",
      "diagram": "what the diagram shows, or empty",
      "documents": "what this portion of the drafts does, or empty",
      "location": "filename and article/section when known, or empty",
      "fix": "concrete change, or empty"
    }}
  ]
}}

Checklist (use these ids when this portion speaks to them):
{checklist}
"""

SYNTH_PROMPT = """\
You are a legal assistant specializing in estate planning.{firm_context}

The drafts were reviewed in parts against the diagram. You will receive a \
merged checklist covering every item, and may also receive the diagram text. \
Produce the final attorney-facing report. You may refine wording. Do not \
change a not_found item to omission or conflict. Do not mention that the \
review was done in parts. Do not invent facts. Do not give legal advice.

Return ONLY valid JSON (no markdown) with:
{{
  "verdict": "congruent" | "mostly_congruent" | "not_congruent" | "incomplete",
  "summary": "one or two sentences",
  "sections": {{
    "<section_id>": {{
      "items": [
        {{
          "id": "<item_id>",
          "status": "match" | "intentional" | "conflict" | "omission" | "not_found" | "unclear",
          "summary": "short specific note",
          "diagram": "what the diagram shows, or empty",
          "documents": "what the drafts do, or empty",
          "location": "filename and article/section when known, or empty",
          "fix": "concrete change, or empty"
        }}
      ]
    }}
  }}
}}

Every item id in the checklist must appear exactly once.

Checklist:
{checklist}
"""

SECTION_SPECS = (
    ("family", "Family Information", (
        ("grantor_names", "Grantor names"),
        ("spouse", "Spouse"),
        ("children", "Children"),
        ("descendants", "Descendants"),
        ("dates_of_birth", "Dates of birth"),
        ("marital_status", "Marital status"),
    )),
    ("fiduciaries", "Fiduciaries", (
        ("initial_trustees", "Initial trustees"),
        ("disability_trustees", "Disability trustees"),
        ("successor_trustees", "Successor trustees"),
        ("personal_representatives", "Personal representatives"),
        ("financial_agents", "Financial agents"),
        ("health_care_agents", "Health care agents"),
        ("successor_health_care_agents", "Successor health care agents"),
        ("trust_protectors", "Trust protectors"),
        ("successor_trust_protectors", "Successor trust protectors"),
    )),
    ("death_distribution", "Death Distribution Structure", (
        ("first_death", "First death provisions"),
        ("survivors_trust", "Survivor's trust"),
        ("marital_trust", "Marital trust"),
        ("family_trust", "Family trust"),
        ("descendants_trusts", "Descendants' trusts"),
        ("continuing_trusts", "Continuing trusts"),
        ("outright_distributions", "Outright distributions"),
        ("remote_contingent", "Remote contingent beneficiaries"),
    )),
    ("specific_gifts", "Specific Gifts", (
        ("charitable_gifts", "Charitable gifts"),
        ("cash_gifts", "Cash gifts"),
        ("percentage_gifts", "Percentage gifts"),
        ("real_estate_gifts", "Real estate gifts"),
        ("tangible_personal_property", "Tangible personal property"),
        ("education_provisions", "Scholarship / education provisions"),
    )),
    ("beneficiaries", "Beneficiary Review", (
        ("primary_beneficiaries", "Primary beneficiaries"),
        ("contingent_beneficiaries", "Contingent beneficiaries"),
        ("remote_contingent_beneficiaries", "Remote contingent beneficiaries"),
        ("per_stirpes", "Per stirpes language"),
        ("powers_of_appointment", "Powers of appointment"),
        ("gst_provisions", "GST provisions"),
    )),
    ("distribution_standards", "Distribution Standards", (
        ("hems", "HEMS"),
        ("pure_discretion", "Pure discretion"),
        ("mandatory_income", "Mandatory income"),
        ("withdrawal_rights", "Withdrawal rights"),
        ("age_based", "Age-based distributions"),
    )),
    ("incapacity", "Incapacity Provisions", (
        ("definition_of_incapacity", "Definition of incapacity"),
        ("who_determines_incapacity", "Who determines incapacity"),
        ("disability_trustee_succession", "Disability trustee succession"),
        ("financial_agent_succession", "Financial agent succession"),
        ("health_care_decision_makers", "Health care decision makers"),
    )),
)

ITEM_LABELS = {
    item_id: label
    for _, _, items in SECTION_SPECS
    for item_id, label in items
}

STATUSES = frozenset({
    "match", "intentional", "conflict", "omission", "not_found", "unclear",
})
STATUS_ALIASES = {
    "matches": "match",
    "extra": "intentional",
    "different": "intentional",
    "probably_intentional": "intentional",
    "not_on_diagram": "intentional",
    "incorrect": "conflict",
    "conflicting": "conflict",
    "missing": "omission",
    "not_in_drafts": "omission",
    "not_in_either": "not_found",
    "absent": "not_found",
    "none": "not_found",
    "unknown": "unclear",
    "incomplete_extract": "unclear",
}
BUCKET_OF = {
    "match": "matches",
    "intentional": "intentional",
    "conflict": "issues",
    "omission": "issues",
    "not_found": "not_found",
    "unclear": "not_found",
}
STATUS_PRIORITY = {
    "conflict": 5,
    "omission": 4,
    "intentional": 3,
    "match": 2,
    "unclear": 1,
    "not_found": 0,
}
VERDICTS = frozenset({
    "congruent", "mostly_congruent", "not_congruent", "incomplete",
})
VERDICT_LABELS = {
    "congruent": "Congruent",
    "mostly_congruent": "Mostly congruent",
    "not_congruent": "Not congruent",
    "incomplete": "Incomplete",
}

_WORD_ONES = "ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE"
_WORD_TEENS = (
    "TEN|ELEVEN|TWELVE|THIRTEEN|FOURTEEN|FIFTEEN|SIXTEEN|"
    "SEVENTEEN|EIGHTEEN|NINETEEN"
)
_WORD_TENS = "TWENTY|THIRTY|FORTY|FIFTY"
_WORD_NUM = (
    rf"(?:{_WORD_TEENS}|{_WORD_TENS}(?:[\s\-](?:{_WORD_ONES}))?|{_WORD_ONES})"
)
_HEADING_RE = re.compile(
    r"^(?:"
    rf"ARTICLE\s+(?:[IVXLCDM]+|\d+|{_WORD_NUM})\b"
    r"|SEC(?:TION)?\.?\s+\d+(?:\.\d+)*\b"
    rf"|ITEM\s+(?:[IVXLCDM]+|\d+)\b"
    r"|SCHEDULE\s+[A-Z0-9]+\b"
    r"|EXHIBIT\s+[A-Z0-9]+\b"
    r"|APPENDIX\s+[A-Z0-9]+\b"
    r")",
    re.IGNORECASE,
)


class CompareError(Exception):
    """User-facing problem with the uploaded files or extracted text."""


def is_pptx_bytes(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return False
    return "ppt/presentation.xml" in names or any(n.startswith("ppt/") for n in names)


def is_docx_bytes(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return False
    return "word/document.xml" in names


def zip_is_oversized(data):
    """True when an Office zip would expand past our in-memory limits."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        return False
    if len(infos) > MAX_ZIP_MEMBERS:
        return True
    total = 0
    for info in infos:
        total += max(info.file_size, 0)
        if total > MAX_UNCOMPRESSED_BYTES:
            return True
    return False


def safe_filename(name):
    cleaned = re.sub(r"[\r\n\t]+", " ", name or "").strip()
    return (cleaned or "Document")[:180]


def _shape_texts(shape):
    try:
        if getattr(shape, "has_table", False):
            rows = []
            for row in shape.table.rows:
                cells = [(getattr(cell, "text", None) or "").strip() for cell in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            return rows
    except Exception:
        pass

    try:
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
            texts = []
            for child in shape.shapes:
                texts.extend(_shape_texts(child))
            return texts
    except Exception:
        pass

    try:
        if getattr(shape, "has_text_frame", False):
            text = (shape.text_frame.text or "").strip()
            if text:
                return [text]
    except Exception:
        pass
    return []


def extract_pptx_text(data):
    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise CompareError(
            "Could not read the PowerPoint. Upload a .pptx file."
        ) from exc

    slides = []
    for index, slide in enumerate(presentation.slides, 1):
        bits = []
        try:
            shapes = list(slide.shapes)
        except Exception:
            shapes = []
        for shape in shapes:
            bits.extend(_shape_texts(shape))

        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes:
                    bits.append("Speaker notes: " + notes)
        except Exception:
            pass

        body = "\n".join(bit for bit in bits if bit)
        if body:
            slides.append(f"--- Slide {index} ---\n{body}")

    return "\n\n".join(slides).strip()


def _table_text(table):
    rows = []
    for row in table.rows:
        try:
            cells = [" ".join((cell.text or "").split()) for cell in row.cells]
        except Exception:
            continue
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_docx_text(data):
    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:
        raise CompareError(
            "Could not read a Word document. Upload a .docx file."
        ) from exc

    parts = []
    for child in document.element.body.iterchildren():
        try:
            if child.tag == qn("w:p"):
                text = Paragraph(child, document).text.strip()
                if text:
                    parts.append(text)
            elif child.tag == qn("w:tbl"):
                table_text = _table_text(Table(child, document))
                if table_text:
                    parts.append(table_text)
        except Exception:
            continue

    seen_hf = set()
    extra = []
    try:
        for section in document.sections:
            for hf in (section.header, section.footer):
                if hf is None:
                    continue
                for paragraph in hf.paragraphs:
                    text = paragraph.text.strip()
                    if text and text not in seen_hf:
                        seen_hf.add(text)
                        extra.append(text)
    except Exception:
        pass
    if extra:
        parts.append("Header/footer: " + " | ".join(extra))

    return "\n".join(parts).strip()


def _checklist_block():
    lines = []
    for section_id, title, items in SECTION_SPECS:
        lines.append(f"{title} ({section_id}):")
        for item_id, label in items:
            lines.append(f"  - {item_id}: {label}")
    return "\n".join(lines)


def _format_prompt(template, firm_config):
    ctx = (firm_config or {}).get("firm_context") or ""
    ctx = f" {ctx.strip()}" if ctx.strip() else ""
    return template.format(firm_context=ctx, checklist=_checklist_block())


def _build_prompt(firm_config):
    return _format_prompt(COMPARE_PROMPT, firm_config)


def _build_user_content(diagram_text, drafts):
    chunks = ["POWERPOINT DIAGRAM\n", diagram_text, "\n\nWORD DRAFTS\n"]
    for name, text in drafts:
        body = text or "(No extractable text.)"
        chunks.append(f"\n--- {safe_filename(name)} ---\n{body}\n")
    return "".join(chunks)


def payload_char_count(diagram_text, drafts):
    return len(_build_user_content(diagram_text, drafts))


def _clean_paragraph(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return text


def _text(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def normalize_status(value):
    raw = _text(value).lower().replace(" ", "_").replace("-", "_")
    if raw in STATUSES:
        return raw
    return STATUS_ALIASES.get(raw, "unclear")


def _blank_item(item_id):
    return {
        "id": item_id,
        "status": "not_found",
        "summary": "Neither the diagram nor the drafts address this.",
        "diagram": "",
        "documents": "",
        "location": "",
        "fix": "",
    }


def _coerce_item(raw, item_id=None):
    if not isinstance(raw, dict):
        return None
    iid = raw.get("id") or item_id
    if iid not in ITEM_LABELS:
        return None
    item = _blank_item(iid)
    item["status"] = normalize_status(raw.get("status"))
    item["summary"] = _text(raw.get("summary"))
    item["diagram"] = _text(raw.get("diagram"))
    item["documents"] = _text(
        raw.get("documents") or raw.get("document") or raw.get("drafts")
    )
    item["location"] = _text(raw.get("location"))
    item["fix"] = _text(raw.get("fix") or raw.get("recommended_fix"))
    if item["status"] == "not_found" and not item["summary"]:
        item["summary"] = "Neither the diagram nor the drafts address this."
    if item["status"] == "unclear" and not item["summary"]:
        item["summary"] = "The extracted text is too thin to decide."
    return item


def _items_from_rows(rows):
    if not isinstance(rows, list):
        return []
    return [item for item in (_coerce_item(row) for row in rows) if item]


def coerce_findings(raw):
    if isinstance(raw, list):
        return _items_from_rows(raw)
    if not isinstance(raw, dict):
        return []

    items = []
    sections = raw.get("sections")
    if isinstance(sections, dict):
        for _section_id, spec in sections.items():
            rows = spec
            if isinstance(spec, dict):
                rows = spec.get("items") or spec.get("findings")
            items.extend(_items_from_rows(rows))
    elif isinstance(sections, list):
        for spec in sections:
            if not isinstance(spec, dict):
                continue
            items.extend(_items_from_rows(spec.get("items")))

    if items:
        return items
    return _items_from_rows(raw.get("findings"))


def _combine_items(first, second):
    out = dict(first)
    for key in ("summary", "diagram", "documents", "fix"):
        if not out.get(key) and second.get(key):
            out[key] = second[key]
    loc_a = first.get("location") or ""
    loc_b = second.get("location") or ""
    if loc_b and loc_b not in loc_a:
        out["location"] = f"{loc_a}; {loc_b}" if loc_a else loc_b
    return out


def _index_items(report):
    sections = (report or {}).get("sections")
    if isinstance(sections, dict):
        values = sections.values()
    elif isinstance(sections, list):
        values = sections
    else:
        return {}
    out = {}
    for section in values:
        if not isinstance(section, dict):
            continue
        for item in section.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                out[item["id"]] = item
    return out


def _count_statuses(report):
    counts = {status: 0 for status in STATUSES}
    for section in report["sections"].values():
        for item in section["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def derive_verdict(report):
    counts = _count_statuses(report)
    issues = counts["conflict"] + counts["omission"]
    decided = counts["match"] + counts["intentional"] + issues
    if decided < 3:
        return "incomplete"
    if issues == 0:
        return "congruent"
    if issues <= 3:
        return "mostly_congruent"
    return "not_congruent"


def default_summary(report):
    verdict = report["verdict"]
    issues = _count_statuses(report)
    n_issues = issues["conflict"] + issues["omission"]
    if verdict == "congruent":
        return (
            "The drafts implement the plan shown on the diagram. "
            "Items neither source addresses are listed as not found."
        )
    if verdict == "incomplete":
        return (
            "The extracted text is too thin to judge whether the drafts "
            "implement the diagram."
        )
    noun = "issue" if n_issues == 1 else "issues"
    if verdict == "mostly_congruent":
        return (
            f"The drafts mostly implement the diagram, with {n_issues} "
            f"{noun} to revise."
        )
    return (
        f"The drafts do not fully implement the diagram. "
        f"{n_issues} missing or conflicting items need attention."
    )


def _report_from_items(by_id, summary=""):
    sections = {}
    for section_id, _title, specs in SECTION_SPECS:
        sections[section_id] = {
            "items": [by_id.get(item_id) or _blank_item(item_id) for item_id, _ in specs],
        }
    report = {"verdict": "", "summary": _text(summary), "sections": sections}
    report["verdict"] = derive_verdict(report)
    if not report["summary"]:
        report["summary"] = default_summary(report)
    return report


def merge_findings(finding_lists):
    """Merge batch sightings. Unseen items become not_found."""
    best = {}
    for findings in finding_lists:
        for item in findings:
            row = dict(item)
            if row["status"] == "not_found":
                row["status"] = "unclear"
            prev = best.get(row["id"])
            if prev is None or STATUS_PRIORITY[row["status"]] > STATUS_PRIORITY[prev["status"]]:
                best[row["id"]] = row
            elif STATUS_PRIORITY[row["status"]] == STATUS_PRIORITY[prev["status"]]:
                best[row["id"]] = _combine_items(prev, row)
    return _report_from_items(best)


def normalize_report(raw):
    """Force a complete checklist and a verdict that ignores not_found."""
    if isinstance(raw, dict) and isinstance(raw.get("sections"), (dict, list)):
        by_id = {}
        for item in coerce_findings(raw):
            prev = by_id.get(item["id"])
            if prev is None or STATUS_PRIORITY[item["status"]] > STATUS_PRIORITY[prev["status"]]:
                by_id[item["id"]] = item
        report = _report_from_items(by_id, summary=_text(raw.get("summary")))
    else:
        report = merge_findings([coerce_findings(raw)])
    if not report["summary"]:
        report["summary"] = default_summary(report)
    return report


def lock_not_found(merged, polished):
    """Keep merge-time not_found; allow the model to refine everything else."""
    merged_by = _index_items(merged)
    polished_norm = normalize_report(polished if isinstance(polished, dict) else {})
    polished_by = _index_items(polished_norm)
    locked = {}
    for item_id in ITEM_LABELS:
        base = merged_by.get(item_id) or _blank_item(item_id)
        over = polished_by.get(item_id)
        if base["status"] == "not_found":
            if over and over["status"] == "not_found" and over.get("summary"):
                locked[item_id] = {**base, "summary": over["summary"]}
            else:
                locked[item_id] = base
        elif over:
            locked[item_id] = {
                **base,
                "summary": over.get("summary") or base["summary"],
                "diagram": over.get("diagram") or base["diagram"],
                "documents": over.get("documents") or base["documents"],
                "location": over.get("location") or base["location"],
                "fix": over.get("fix") or base["fix"],
            }
        else:
            locked[item_id] = base
    summary = ""
    if isinstance(polished, dict):
        summary = _text(polished.get("summary"))
    return _report_from_items(locked, summary=summary or polished_norm.get("summary") or "")


def present_report(report):
    """Shape the checklist for the right-hand panel."""
    if not isinstance(report, dict) or not isinstance(report.get("sections"), dict):
        report = normalize_report(report)
    elif report["sections"] and "items" not in next(iter(report["sections"].values()), {}):
        report = normalize_report(report)
    else:
        report = {
            **report,
            "verdict": report.get("verdict") or derive_verdict(report),
            "summary": report.get("summary") or default_summary(report),
        }

    sections = []
    for section_id, title, _specs in SECTION_SPECS:
        spec = report["sections"].get(section_id) or {"items": []}
        buckets = {"matches": [], "intentional": [], "issues": [], "not_found": []}
        for item in spec.get("items") or []:
            item_id = item.get("id")
            bucket = BUCKET_OF.get(item.get("status"))
            if item_id not in ITEM_LABELS or not bucket:
                continue
            buckets[bucket].append({
                "id": item_id,
                "label": ITEM_LABELS[item_id],
                "status": item.get("status"),
                "summary": item.get("summary") or "",
                "diagram": item.get("diagram") or "",
                "documents": item.get("documents") or "",
                "location": item.get("location") or "",
                "fix": item.get("fix") or "",
            })
        sections.append({
            "id": section_id,
            "title": title,
            "issue_count": len(buckets["issues"]),
            **buckets,
        })
    verdict = report["verdict"] if report.get("verdict") in VERDICTS else derive_verdict(report)
    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "summary": report.get("summary") or default_summary({**report, "verdict": verdict}),
        "sections": sections,
    }


def _is_structural_heading(line):
    text = (line or "").strip()
    if not text or len(text) > 160:
        return False
    return bool(_HEADING_RE.match(text))


def _heading_label(line):
    text = " ".join((line or "").split())
    if len(text) > 72:
        text = text[:69].rstrip() + "..."
    return text


def _split_into_sections(text):
    sections = []
    current_title = ""
    current = []
    for line in (text or "").split("\n"):
        if _is_structural_heading(line) and current:
            sections.append((current_title, "\n".join(current).strip()))
            current_title = _heading_label(line)
            current = [line]
        else:
            if not current and _is_structural_heading(line):
                current_title = _heading_label(line)
            current.append(line)
    body = "\n".join(current).strip()
    if body:
        sections.append((current_title, body))
    return sections


def _window_split(text, max_chars):
    text = text or ""
    if max_chars <= 0:
        return [text] if text else []
    if len(text) <= max_chars:
        return [text] if text else []

    overlap = min(CHUNK_OVERLAP, max(0, max_chars // 8))
    parts = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            cut = text.rfind("\n", start + max(1, max_chars // 2), end)
            if cut == -1:
                cut = text.rfind(" ", start + max(1, max_chars // 2), end)
            if cut != -1:
                end = cut
        piece = text[start:end].strip()
        if piece:
            parts.append(piece)
        if end >= length:
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
    return parts


def _pack_sections(sections, max_chars):
    packed = []
    cur_start = ""
    cur_end = ""
    cur_parts = []
    cur_len = 0

    def flush():
        nonlocal cur_start, cur_end, cur_parts, cur_len
        if not cur_parts:
            return
        packed.append((cur_start, cur_end, "\n".join(cur_parts)))
        cur_start = ""
        cur_end = ""
        cur_parts = []
        cur_len = 0

    for title, body in sections:
        body = (body or "").strip()
        if not body:
            continue
        extra = len(body) + (1 if cur_parts else 0)
        if cur_parts and cur_len + extra > max_chars:
            flush()
        if not cur_parts:
            cur_start = title
        if title:
            cur_end = title
        cur_parts.append(body)
        cur_len += extra
        if cur_len > max_chars:
            flush()
    flush()
    return packed


def chunk_draft_text(text, max_chars):
    """Split one draft so each piece is at most max_chars.

    Returns a list of (start_heading, end_heading, body). Prefers article /
    section boundaries; falls back to overlapping windows.
    """
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [("", "", text)]

    sections = _split_into_sections(text)
    heading_count = sum(1 for title, _ in sections if title)
    pieces = []
    if heading_count >= 1:
        for start, end, body in _pack_sections(sections, max_chars):
            if len(body) <= max_chars:
                pieces.append((start, end, body))
            else:
                for window in _window_split(body, max_chars):
                    pieces.append((start, end, window))
    else:
        for window in _window_split(text, max_chars):
            pieces.append(("", "", window))
    return pieces or [("", "", w) for w in _window_split(text, max_chars)] or [("", "", text)]


def _span_label(start, end):
    start = (start or "").strip()
    end = (end or "").strip()
    if start and end and start != end:
        return f": {start} - {end}"
    if start or end:
        return f": {start or end}"
    return ""


def _part_label(name, index, total, start, end):
    base = safe_filename(name)
    if total <= 1:
        return base
    return f"{base} (part {index} of {total}{_span_label(start, end)})"


def _max_body_chars(diagram_text, name):
    probe = safe_filename(name) + ("x" * _PART_LABEL_PAD)
    used = payload_char_count(diagram_text, [(probe, "")])
    return MAX_TOTAL_CHARS - used


def _fits(diagram_text, drafts):
    return payload_char_count(diagram_text, drafts) <= MAX_TOTAL_CHARS


def plan_compare_batches(diagram_text, drafts):
    """Pack drafts into payload-sized batches; chunk any single oversized draft."""
    leftover = _max_body_chars(diagram_text, "Draft")
    if leftover < MIN_CHUNK_CHARS:
        raise CompareError(
            "The PowerPoint is too large to compare against the drafts."
        )

    batches = []
    current = []
    for name, text in drafts:
        text = (text or "").strip()
        if not text:
            continue
        candidate = current + [(name, text)]
        if _fits(diagram_text, candidate):
            current = candidate
            continue
        if current:
            batches.append(current)
            current = []
        if _fits(diagram_text, [(name, text)]):
            current = [(name, text)]
            continue
        max_body = _max_body_chars(diagram_text, name)
        if max_body < 1:
            raise CompareError(
                "The PowerPoint is too large to compare against the drafts."
            )
        pieces = chunk_draft_text(text, max_body)
        total = len(pieces)
        for index, (start, end, body) in enumerate(pieces, 1):
            label = _part_label(name, index, total, start, end)
            if not _fits(diagram_text, [(label, body)]):
                tight = _max_body_chars(diagram_text, label)
                windows = _window_split(body, max(1, tight))
                w_total = len(windows)
                for w_index, window in enumerate(windows, 1):
                    w_label = _part_label(name, w_index, w_total, start, end)
                    if not _fits(diagram_text, [(w_label, window)]):
                        room = _max_body_chars(diagram_text, w_label)
                        window = window[: max(0, room)]
                    if window:
                        batches.append([(w_label, window)])
            else:
                batches.append([(label, body)])
    if current:
        batches.append(current)
    if not batches:
        raise CompareError("No readable text was found in the Word document(s).")
    if len(batches) > MAX_COMPARE_PASSES:
        raise CompareError(
            "These documents are too large to compare in one run. "
            "Try fewer Word documents."
        )
    return batches


def _compact_report(report):
    sections = {}
    for section_id, _title, _specs in SECTION_SPECS:
        spec = report["sections"][section_id]
        sections[section_id] = {
            "items": [
                {
                    "id": item["id"],
                    "status": item["status"],
                    "summary": item["summary"],
                    "diagram": item["diagram"],
                    "documents": item["documents"],
                    "location": item["location"],
                    "fix": item["fix"],
                }
                for item in spec["items"]
            ],
        }
    return {"summary": report.get("summary") or "", "sections": sections}


def _build_synth_content(merged, diagram_text):
    payload = json.dumps(_compact_report(merged), indent=2)
    parts = [
        "MERGED CHECKLIST (from reviewing the drafts in parts)\n",
        payload,
    ]
    diagram_text = (diagram_text or "").strip()
    if diagram_text and len(payload) + len(diagram_text) + 80 <= MAX_TOTAL_CHARS:
        parts.append("\n\nPOWERPOINT DIAGRAM\n")
        parts.append(diagram_text)
    return "".join(parts)


def _parse_compare_json(text):
    from doc_separator import _extract_json

    try:
        return _extract_json(text)
    except ValueError:
        pass
    obj_at = text.find("{")
    arr_at = text.find("[")
    if obj_at == -1 and arr_at == -1:
        raise ValueError("No JSON object in model response")
    if arr_at != -1 and (obj_at == -1 or arr_at < obj_at):
        end = text.rfind("]")
        snippet = text[arr_at:end + 1] if end > arr_at else text[arr_at:]
    else:
        end = text.rfind("}")
        snippet = text[obj_at:end + 1] if end > obj_at else text[obj_at:]
    return _extract_json(snippet)


def _complete(
    client,
    *,
    system,
    user,
    tool,
    model,
    firm_id,
    expect_json=False,
):
    from ai_logger import log_ai_call, extract_xai_usage, completion_details

    call_start = time.time()
    details = None
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = _clean_paragraph(response.choices[0].message.content)
        details = completion_details(response, raw)
        if not raw:
            log_ai_call(
                provider="openai",
                model=model,
                tool=tool,
                status="error",
                execution_ms=int((time.time() - call_start) * 1000),
                notes="Empty comparison text",
                firm_id=firm_id,
                **extract_xai_usage(response),
            )
            raise CompareError("The comparison came back empty. Please try again.")
        parsed = None
        if expect_json:
            try:
                parsed = _parse_compare_json(raw)
            except Exception:
                log_ai_call(
                    provider="openai",
                    model=model,
                    tool=tool,
                    status="error",
                    execution_ms=int((time.time() - call_start) * 1000),
                    notes="JSON parse failed\n" + traceback.format_exc(),
                    firm_id=firm_id,
                    **extract_xai_usage(response),
                )
                raise CompareError(
                    "The comparison came back in an unexpected format. Please try again."
                )
            if not isinstance(parsed, (dict, list)):
                log_ai_call(
                    provider="openai",
                    model=model,
                    tool=tool,
                    status="error",
                    execution_ms=int((time.time() - call_start) * 1000),
                    notes="JSON was not an object or array",
                    firm_id=firm_id,
                    **extract_xai_usage(response),
                )
                raise CompareError(
                    "The comparison came back in an unexpected format. Please try again."
                )
        log_ai_call(
            provider="openai",
            model=model,
            tool=tool,
            status="success",
            execution_ms=int((time.time() - call_start) * 1000),
            firm_id=firm_id,
            **extract_xai_usage(response),
        )
        return parsed if expect_json else raw
    except CompareError:
        raise
    except Exception as exc:
        log_ai_call(
            provider="openai",
            model=model,
            tool=tool,
            status="error",
            execution_ms=int((time.time() - call_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        if details:
            exc.details = details
        raise


def compare_diagram_to_drafts(
    diagram_text,
    drafts,
    client,
    *,
    model=None,
    firm_id=None,
    firm_config=None,
    on_progress=None,
):
    """Compare Word drafts to the PowerPoint diagram.

    drafts is a list of (filename, extracted_text).
    Returns a structured report for the right-hand panel. Oversized sets are
    compared in batches; a single long draft is split on articles/sections
    (or overlapping windows) and findings are merged before the final report.
    """
    diagram_text = (diagram_text or "").strip()
    if not diagram_text:
        raise CompareError(
            "No readable text was found in the PowerPoint. "
            "If the diagram is image-only, this version cannot compare it."
        )

    cleaned = []
    for name, text in drafts:
        cleaned.append((safe_filename(name), (text or "").strip()))
    if not cleaned:
        raise CompareError("Upload at least one Word document.")
    if not any(text for _, text in cleaned):
        raise CompareError(
            "No readable text was found in the Word document(s)."
        )

    batches = plan_compare_batches(diagram_text, cleaned)
    log.info(
        "Comparing diagram (%d chars) against %d draft(s) in %d pass(es)",
        len(diagram_text),
        len(cleaned),
        len(batches),
    )

    model = model or os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")
    if len(batches) == 1:
        if on_progress:
            on_progress("compare")
        raw = _complete(
            client,
            system=_format_prompt(COMPARE_PROMPT, firm_config),
            user=_build_user_content(diagram_text, batches[0]),
            tool=TOOL_KEY,
            model=model,
            firm_id=firm_id,
            expect_json=True,
        )
        return present_report(normalize_report(raw))

    finding_lists = []
    total = len(batches)
    for index, batch in enumerate(batches, 1):
        if on_progress:
            on_progress(f"batch {index}/{total}")
        raw = _complete(
            client,
            system=_format_prompt(BATCH_PROMPT, firm_config),
            user=_build_user_content(diagram_text, batch),
            tool=BATCH_TOOL_KEY,
            model=model,
            firm_id=firm_id,
            expect_json=True,
        )
        finding_lists.append(coerce_findings(raw))

    merged = merge_findings(finding_lists)
    if on_progress:
        on_progress("synthesize")
    polished = _complete(
        client,
        system=_format_prompt(SYNTH_PROMPT, firm_config),
        user=_build_synth_content(merged, diagram_text),
        tool=TOOL_KEY,
        model=model,
        firm_id=firm_id,
        expect_json=True,
    )
    return present_report(lock_not_found(merged, polished))
