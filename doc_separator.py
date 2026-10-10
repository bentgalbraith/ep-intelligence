"""PDF document separation: OCR via Google Document AI, boundary detection via Grok."""

import calendar
import io
import json
import logging
import os
import re
import time
import traceback
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.api_core.client_options import ClientOptions
from google.cloud import documentai_v1 as documentai
from google.oauth2 import service_account
from pypdf import PdfReader, PdfWriter

log = logging.getLogger("doc_separator")
log.setLevel(logging.DEBUG)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[doc_separator] %(message)s"))
    log.addHandler(_h)

PAGES_PER_CHUNK = 5

DOC_SEPARATOR_PROMPT = """\
You will receive OCR text extracted page-by-page from a scanned PDF that contains \
multiple estate-planning documents merged into one file. Each page is labeled with \
its page number.

Identify every complete document within the scan. A "document" means the primary \
instrument together with all of its parts — exhibits, schedules, addenda, \
attachments, and any mostly-blank pages (stamps, seals, notary blocks) that \
immediately follow it. These parts are NEVER listed as separate entries; they are \
included in the page range of their parent document.

For each document provide:
1. start_page – first page number (1-based)
2. end_page – last page number (1-based)
3. document_type – a short Title Case label derived from the document's actual \
title in the OCR. Use the core document type only: drop the client's name, \
dates, parenthetical subtitles, and legal qualifiers. For example, \
"ADVANCE DIRECTIVE FOR HEALTH CARE (LIVING WILL AND DESIGNATION OF HEALTH \
CARE SURROGATE(S)) OF LOYD A. WOLFLEY" becomes "Advance Directive For Health Care". \
Do NOT paraphrase into a different term (e.g. do not change "Advance Directive" \
to "Healthcare Proxy").
4. client_first_name – primary client's first name
5. client_last_name – primary client's last name
6. document_date – execution / signing date in M-D-YY format (e.g. "4-13-26"). \
If not found, set to null.

Return ONLY valid JSON (no markdown fences, no commentary):
{
  "documents": [
    {
      "start_page": 1,
      "end_page": 5,
      "document_type": "Revocable Trust",
      "client_first_name": "Mary",
      "client_last_name": "Smith",
      "document_date": "4-13-26"
    }
  ]
}

Rules:
- Every page must belong to exactly one document (no gaps, no overlaps).
- Pages within a document must be contiguous.
- Order the array by start_page.
- Always include both start_page and end_page, even for single-page documents \
(e.g. start_page: 5, end_page: 5).
- Always include all six fields for every document — never omit any.
- Include cover sheets and TOCs with their parent document.
- Capitalize names properly.
- Pages with scanned ID cards (identified by OCR references to a state-issued ID, \
driver's license, or passport) must not be skipped. Name them using standard \
conventions with a specific document_type like "Florida ID" or "Indiana Driver's \
License". However, do NOT label a page as an ID card unless the OCR text clearly \
contains ID-related text. A mostly-blank page with only a stamp or seal is not \
an ID card — it is part of the preceding document.
- Uploads often contain documents for both a husband and a wife. Many documents \
are specific to only one person — do not merge distinct documents pertaining to \
two different people into one entry.
- A HIPAA authorization (e.g. "Authorization for Release of Protected Health \
Information") is always its own standalone document, never part of a living will \
or advance directive.
- A Certification of Trust (which often states that relevant portions of the trust \
agreement are attached) plus any attached trust excerpts together form one \
document — the Certification of Trust.
- An Operating Agreement (including any Amended and Restated Operating Agreement) \
and all of its schedules and exhibits form one single document — do not separate them.
- For Transfer on Death Beneficiary Designations, include the name of the business \
entity or account in the document_type (e.g. "Transfer on Death Beneficiary \
Designation - Smith Family LLC"). The entity name is not the client's name and \
must not be stripped.
- For business entity documents (Operating Agreements, Articles of Organization, \
etc.), client_first_name and client_last_name should be the primary member or \
organizer — infer this from the document content or from other documents in the \
same upload that reference the same entity.
"""


def _fix_escapes(s):
    """Fix invalid JSON backslash escapes by doubling lone backslashes."""
    import re
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


def _extract_json(text):
    """Extract JSON from model output, repairing truncated closing brackets."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    try:
        return json.loads(_fix_escapes(text))
    except (json.JSONDecodeError, ValueError):
        pass

    for suffix in ("}", "]}", "]}"):
        try:
            return json.loads(text + suffix)
        except (json.JSONDecodeError, ValueError):
            pass

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            for suffix in ("}", "]}", "]}"):
                try:
                    return json.loads(cleaned + suffix)
                except (json.JSONDecodeError, ValueError):
                    pass

    raise ValueError(f"No valid JSON in model response: {text[:500]}")


def _get_documentai_client():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS", "")
    project_id = os.environ["GOOGLE_PROJECT_ID"]
    location = os.environ.get("GOOGLE_LOCATION", "us")
    processor_id = os.environ["GOOGLE_PROCESSOR_ID"]

    if not all([project_id, processor_id]):
        raise RuntimeError(
            "Set GOOGLE_PROJECT_ID and GOOGLE_PROCESSOR_ID environment variables."
        )

    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")

    if creds_raw:
        info = json.loads(creds_raw)
        creds = service_account.Credentials.from_service_account_info(info)
        client = documentai.DocumentProcessorServiceClient(
            credentials=creds, client_options=opts
        )
    else:
        client = documentai.DocumentProcessorServiceClient(client_options=opts)

    name = client.processor_path(project_id, location, processor_id)
    return client, name


def _page_text(document, page):
    """Extract the full text of one page from a Document AI response."""
    segments = page.layout.text_anchor.text_segments
    if not segments:
        return ""
    return "".join(document.text[seg.start_index : seg.end_index] for seg in segments)


def _ocr_pages(pdf_content, firm_id=None):
    """OCR every page in chunks; return (dict[page_number -> text], total_pages)."""
    from ai_logger import log_ai_call

    reader = PdfReader(io.BytesIO(pdf_content))
    total = len(reader.pages)
    ocr_start = time.time()
    client, processor_name = _get_documentai_client()

    texts = {}
    for start in range(0, total, PAGES_PER_CHUNK):
        end = min(start + PAGES_PER_CHUNK, total)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])

        buf = io.BytesIO()
        writer.write(buf)

        try:
            result = client.process_document(
                request=documentai.ProcessRequest(
                    name=processor_name,
                    raw_document=documentai.RawDocument(
                        content=buf.getvalue(), mime_type="application/pdf"
                    ),
                )
            )
        except Exception:
            log_ai_call(
                provider="google_documentai", tool="doc_separator_ocr", status="error",
                pages_processed=total,
                execution_ms=int((time.time() - ocr_start) * 1000),
                notes=traceback.format_exc(),
                firm_id=firm_id,
            )
            raise

        for local_idx, page in enumerate(result.document.pages):
            texts[start + local_idx + 1] = _page_text(result.document, page)

    ocr_elapsed = time.time() - ocr_start

    log_ai_call(
        provider="google_documentai", tool="doc_separator_ocr", status="success",
        pages_processed=total,
        execution_ms=int(ocr_elapsed * 1000),
        firm_id=firm_id,
    )
    total_chars = sum(len(t) for t in texts.values())
    empty_pages = [pn for pn in range(1, total + 1) if not texts.get(pn, "").strip()]
    log.info("OCR complete: %d pages, %d chars, %.1fs", total, total_chars, ocr_elapsed)
    if empty_pages:
        log.warning("OCR returned empty text for pages: %s", empty_pages)

    return texts, total


def _identify_documents(client, page_texts, total_pages, model, firm_id=None, extra_rules=""):
    """Ask Grok to identify document boundaries and metadata."""
    from ai_logger import log_ai_call, extract_xai_usage, completion_details

    pages_block = ""
    for pn in range(1, total_pages + 1):
        txt = page_texts.get(pn, "").strip()
        pages_block += f"\n--- PAGE {pn} ---\n{txt}\n"

    system_prompt = DOC_SEPARATOR_PROMPT
    if extra_rules:
        system_prompt += f"\n\nAdditional rules from the firm:\n{extra_rules}"

    call_start = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": pages_block},
            ],
        )
    except Exception:
        log_ai_call(
            provider="openai", model=model, tool="doc_separator", status="error",
            execution_ms=int((time.time() - call_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        raise
    call_elapsed = time.time() - call_start

    usage = resp.usage
    finish_reason = resp.choices[0].finish_reason
    log.info(
        "OpenAI: %.1fs, %s/%s tokens (prompt/completion), finish_reason=%s",
        call_elapsed,
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
        finish_reason,
    )
    if finish_reason != "stop":
        log.warning(
            "MODEL DID NOT FINISH NORMALLY — finish_reason='%s' (likely truncated!)",
            finish_reason,
        )

    raw = resp.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        parsed = _extract_json(raw)
    except Exception as e:
        log_ai_call(
            provider="openai", model=model, tool="doc_separator", status="error",
            execution_ms=int(call_elapsed * 1000),
            notes=f"JSON parse failed; finish_reason={finish_reason}\n{traceback.format_exc()}",
            firm_id=firm_id,
            **extract_xai_usage(resp),
        )
        e.details = completion_details(resp, raw)
        raise

    log_ai_call(
        provider="openai", model=model, tool="doc_separator", status="success",
        execution_ms=int(call_elapsed * 1000),
        firm_id=firm_id,
        **extract_xai_usage(resp),
    )

    if "documents" in parsed:
        docs = parsed["documents"]
        log.info("Parsed %d documents", len(docs))
        return docs
    if isinstance(parsed, list):
        log.info("Parsed %d documents", len(parsed))
        return parsed

    err = ValueError(
        f"Unexpected JSON structure (keys: {list(parsed.keys())}): {raw[:500]}"
    )
    err.details = completion_details(resp, raw)
    raise err


def _build_filename(doc_info, fmt=None):
    last = doc_info.get("client_last_name") or "Unknown"
    first = doc_info.get("client_first_name") or "Client"
    dtype = doc_info.get("document_type") or "Document"
    date = doc_info.get("document_date") or "(Undated)"

    if fmt:
        name = fmt.format(last=last, first=first, type=dtype, date=date) + ".pdf"
    else:
        name = f"{last}, {first} - {dtype} {date}.pdf"
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _page_num(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def summarize_split(documents, filename_fmt=None):
    """Compact one-line summaries of a split, for redo logging. No PDF/OCR."""
    lines = []
    for doc in documents or []:
        if not isinstance(doc, dict):
            continue
        start = _page_num(doc.get("start_page"))
        end = _page_num(doc.get("end_page"))
        if start is None and end is None:
            pages = "?"
        elif end is None or start == end:
            pages = str(start)
        else:
            pages = f"{start}–{end}"
        dtype = str(doc.get("document_type") or "Document").strip() or "Document"
        try:
            filename = _build_filename(doc, fmt=filename_fmt)
        except Exception:
            filename = "(filename error)"
        lines.append(f"{pages} | {dtype} | {filename}")
    return lines


def _split_and_zip(pdf_content, documents, filename_fmt=None):
    """Split a PDF according to document boundaries and return a ZIP buffer."""
    reader = PdfReader(io.BytesIO(pdf_content))
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in documents:
            writer = PdfWriter()
            for p in range(doc["start_page"] - 1, doc["end_page"]):
                if p < len(reader.pages):
                    writer.add_page(reader.pages[p])
            pdf_buf = io.BytesIO()
            writer.write(pdf_buf)
            zf.writestr(_build_filename(doc, fmt=filename_fmt), pdf_buf.getvalue())
    zip_buf.seek(0)
    return zip_buf


def separate_documents(pdf_content, client, model=None, firm_id=None, firm_config=None):
    """OCR -> detect boundaries -> split -> zip.

    Returns (BytesIO zip, doc list, page_texts, total_pages).
    """
    total_start = time.time()
    model = model or os.environ.get("DOC_SEPARATOR_MODEL", "gpt-5.6-terra")
    firm_config = firm_config or {}
    log.info("Starting: model=%s, PDF=%d bytes", model, len(pdf_content))

    page_texts, total_pages = _ocr_pages(pdf_content, firm_id=firm_id)
    documents = _identify_documents(
        client, page_texts, total_pages, model,
        firm_id=firm_id,
        extra_rules=firm_config.get("doc_separator_rules", ""),
    )

    filename_fmt = firm_config.get("doc_filename_format")
    zip_buf = _split_and_zip(pdf_content, documents, filename_fmt=filename_fmt)
    total_elapsed = time.time() - total_start
    log.info("Complete: %d docs, %.1fs total", len(documents), total_elapsed)
    return zip_buf, documents, page_texts, total_pages


_REDO_SUFFIX = """
IMPORTANT CORRECTION: You previously analyzed this document and produced the \
following split:

{previous_result}

The user reviewed your work and has the following feedback:
"{feedback}"

Re-analyze the OCR text below and produce a corrected result. Apply the user's \
feedback to fix the specific issues they identified while keeping everything \
else that was correct. Return the full corrected JSON in the same format.
"""


def _build_redo_prompt(previous_result, feedback, extra_rules=""):
    prompt = DOC_SEPARATOR_PROMPT
    if extra_rules:
        prompt += f"\n\nAdditional rules from the firm:\n{extra_rules}"
    return prompt + _REDO_SUFFIX.format(
        previous_result=previous_result, feedback=feedback
    )


def redo_with_feedback(pdf_content, client, page_texts, total_pages,
                       previous_documents, feedback, model=None, firm_id=None, firm_config=None):
    """Re-run boundary detection with user feedback, skipping OCR.

    Returns (BytesIO zip, doc list).
    """
    from ai_logger import log_ai_call, extract_xai_usage, completion_details

    total_start = time.time()
    model = model or os.environ.get("DOC_SEPARATOR_MODEL", "gpt-5.6-terra")
    firm_config = firm_config or {}
    log.info("Redo with feedback: model=%s, feedback=%r", model, feedback[:200])

    previous_json = json.dumps({"documents": previous_documents}, indent=2)
    system_prompt = _build_redo_prompt(
        previous_json, feedback,
        extra_rules=firm_config.get("doc_separator_rules", ""),
    )

    pages_block = ""
    for pn in range(1, total_pages + 1):
        txt = page_texts.get(pn, "").strip()
        pages_block += f"\n--- PAGE {pn} ---\n{txt}\n"

    call_start = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": pages_block},
            ],
        )
    except Exception:
        log_ai_call(
            provider="openai", model=model, tool="doc_separator_redo", status="error",
            execution_ms=int((time.time() - call_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        raise
    call_elapsed = time.time() - call_start

    usage = resp.usage
    finish_reason = resp.choices[0].finish_reason
    log.info(
        "Redo OpenAI: %.1fs, %s/%s tokens (prompt/completion), finish_reason=%s",
        call_elapsed,
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
        finish_reason,
    )
    if finish_reason != "stop":
        log.warning(
            "MODEL DID NOT FINISH NORMALLY — finish_reason='%s' (likely truncated!)",
            finish_reason,
        )

    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        parsed = _extract_json(raw)
    except Exception as e:
        log_ai_call(
            provider="openai", model=model, tool="doc_separator_redo", status="error",
            execution_ms=int(call_elapsed * 1000),
            notes=f"JSON parse failed; finish_reason={finish_reason}\n{traceback.format_exc()}",
            firm_id=firm_id,
            **extract_xai_usage(resp),
        )
        e.details = completion_details(resp, raw)
        raise

    log_ai_call(
        provider="openai", model=model, tool="doc_separator_redo", status="success",
        execution_ms=int(call_elapsed * 1000),
        firm_id=firm_id,
        **extract_xai_usage(resp),
    )
    if "documents" in parsed:
        documents = parsed["documents"]
    elif isinstance(parsed, list):
        documents = parsed
    else:
        err = ValueError(
            f"Unexpected JSON structure (keys: {list(parsed.keys())}): {raw[:500]}"
        )
        err.details = completion_details(resp, raw)
        raise err

    log.info("Redo parsed %d documents", len(documents))
    filename_fmt = firm_config.get("doc_filename_format")
    zip_buf = _split_and_zip(pdf_content, documents, filename_fmt=filename_fmt)
    total_elapsed = time.time() - total_start
    log.info("Redo complete: %d docs, %.1fs total", len(documents), total_elapsed)
    return zip_buf, documents


_QPRT_TYPE_RE = re.compile(r"\bqprt\b|personal residence trust", re.IGNORECASE)
_MDY_RE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})\s*$")
_EASTERN = ZoneInfo("America/New_York")

QPRT_TERM_PROMPT = """\
You will receive OCR text from one or more Qualified Personal Residence Trusts \
(QPRTs). Each document is labeled DOCUMENT N and each page is labeled PAGE n.

For every document, find the clause that sets the length of the initial term \
(also called the retained term or QPRT term). Also find the date the document \
was signed or executed, if that date is actually printed in the OCR.

Return ONLY valid JSON (no markdown fences, no commentary):
{
  "qprts": [
    {
      "document_index": 0,
      "source_page": 4,
      "term_years": 3,
      "term_months": 0,
      "signed_date": "4-13-26",
      "first_names": ["Mary"],
      "last_name": "Smith"
    }
  ]
}

Rules:
- Include one object for every DOCUMENT in the input. document_index is the \
DOCUMENT number, not a page number.
- source_page is the page number on the PAGE label where the term length is stated.
- term_years and term_months are integers. "3 years" is 3 and 0. "18 months" is \
0 and 18. "2 years and 6 months" is 2 and 6. A spelled-out number counts \
("three (3) years" is 3 and 0).
- If the term ends on the earlier of a fixed period or death, use the fixed \
period. Do not account for death.
- If you cannot find a fixed period measured in years or months, set term_years \
and term_months to null.
- signed_date is the signature or execution date in M-D-YY. If the OCR does not \
contain one, set signed_date to null. Do not guess, and do not use a date that \
only says when property was transferred.
- first_names lists each grantor or settlor first name on that document. \
last_name is the primary grantor's last name.
- Do not calculate an end date.
"""


def _is_qprt(doc):
    return bool(_QPRT_TYPE_RE.search(str((doc or {}).get("document_type") or "")))


def _as_int(value):
    if value is None or value is False or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _span(doc, total_pages):
    start = _page_num(doc.get("start_page"))
    end = _page_num(doc.get("end_page"))
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if start < 1 or end < start or start > total_pages:
        return None
    return start, min(end, total_pages)


def _has_page_text(page_texts, start, end):
    return any((page_texts.get(pn) or "").strip() for pn in range(start, end + 1))


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_LONG_DATE_RE = re.compile(r"^\s*([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\s*$")


def _date_or_none(year, month, day):
    if year < 100:
        year += 1900 if year >= 70 else 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_mdy(value):
    """Parse M-D-YY, M-D-YYYY, or 'April 13, 2026'.

    Two-digit years 70-99 are 1970-1999.
    """
    if not isinstance(value, str):
        return None
    match = _MDY_RE.match(value)
    if match:
        return _date_or_none(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    match = _LONG_DATE_RE.match(value)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower().rstrip("."))
    if not month:
        return None
    return _date_or_none(int(match.group(3)), month, int(match.group(2)))


def add_years_months(start, years, months):
    """Anniversary date. Feb 29 lands on Feb 28 when the target year is not a leap year."""
    month_index = start.month - 1 + int(months or 0)
    year = start.year + int(years or 0) + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _long_date(value):
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _term_parts(years, months):
    if years is None or months is None:
        return None
    if years < 0 or months < 0 or years > 100 or months > 1200:
        return None
    if years == 0 and months == 0:
        return None
    parts = []
    if years:
        parts.append(f"{years} Year" if years == 1 else f"{years} Years")
    if months:
        parts.append(f"{months} Month" if months == 1 else f"{months} Months")
    return " ".join(parts)


def _name_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    names = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _join_names(names):
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]


def _ics_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_fold(line):
    data = line.encode("utf-8")
    parts = []
    while len(data) > 73:
        cut = 73
        while cut > 0 and (data[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(data[:cut].decode("utf-8"))
        data = b" " + data[cut:]
    parts.append(data.decode("utf-8"))
    return "\r\n".join(parts)


def _calendar_title(last, firsts, term_label, used_today):
    basis = "From Today" if used_today else "Since Signing"
    return (
        f"{last}, {_join_names(firsts)}: "
        f"QPRT Initial Term Expiration ({term_label} {basis})"
    )


def build_qprt_ics(*, title, description, end, uid):
    """All-day event on the end date, with a display alarm at 9 AM 30 days before.

    DTEND is the next day because an all-day event's end is exclusive.
    RFC 5545 durations have no month unit, so the alarm is 29 days 15 hours
    before the event's midnight start.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start = end.strftime("%Y%m%d")
    finish = (end + timedelta(days=1)).strftime("%Y%m%d")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//EP Intelligence//QPRT Term//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start}",
        f"DTEND;VALUE=DATE:{finish}",
        f"SUMMARY:{_ics_escape(title)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        "BEGIN:VALARM",
        "TRIGGER:-P29DT15H",
        "ACTION:DISPLAY",
        "DESCRIPTION:QPRT initial term expires in 30 days",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


_NO_TERM = "No fixed initial term was found in this QPRT, so no end date was calculated."
_NO_TEXT = "No readable text was found on this QPRT's pages, so no end date was calculated."
_NO_ANSWER = "The QPRT could not be read this time. Please try again."


def _error_row(filename, message=_NO_TERM):
    return {"document_name": filename, "error": message}


def _row_from_item(item, doc, filename, span, today):
    years = _as_int(item.get("term_years"))
    months = _as_int(item.get("term_months"))
    if years is None and months is None:
        return _error_row(filename)
    if years is None:
        years = 0
    if months is None:
        months = 0
    term_label = _term_parts(years, months)
    if not term_label:
        return _error_row(filename)

    signed = parse_mdy(item.get("signed_date")) or parse_mdy(doc.get("document_date"))
    used_today = signed is None
    start = today if used_today else signed
    end = add_years_months(start, years, months)

    last = str(item.get("last_name") or "").strip() or (doc.get("client_last_name") or "Unknown")
    firsts = _name_list(item.get("first_names"))
    if not firsts:
        firsts = [str(doc.get("client_first_name") or "Client").strip() or "Client"]

    source_page = _as_int(item.get("source_page"))
    if source_page is None or source_page < span[0] or source_page > span[1]:
        source_page = None

    title = _calendar_title(last, firsts, term_label, used_today)
    if used_today:
        date_note = "Today's date, because no signed date was found on the document."
    else:
        date_note = ""
    description = "\n".join([
        title,
        f"Document: {filename}",
        f"Page: {source_page if source_page else 'not identified'}",
        f"Term: {term_label}",
        f"Date used: {_long_date(start)}" + (f" ({date_note})" if date_note else ""),
        f"End date: {_long_date(end)}",
        "Reminder: 30 days before.",
    ])
    uid = f"qprt-{uuid.uuid4().hex}@ep-intelligence"
    safe_first = _join_names(firsts)
    ics_filename = re.sub(
        r'[<>:"/\\|?*]', "_",
        f"{last}, {safe_first} - QPRT Initial Term Expiration {end.month}-{end.day}-{str(end.year)[2:]}.ics",
    )
    return {
        "document_name": filename,
        "source_page": source_page,
        "term_label": term_label,
        "date_used": _long_date(start),
        "date_used_kind": "today" if used_today else "signed",
        "date_note": date_note,
        "end_date": _long_date(end),
        "calendar_title": title,
        "ics_filename": ics_filename,
        "ics": build_qprt_ics(title=title, description=description, end=end, uid=uid).encode("utf-8"),
        "error": None,
    }


def _qprt_user_message(entries, page_texts):
    blocks = []
    for index, (doc, start, end) in enumerate(entries):
        last = doc.get("client_last_name") or ""
        first = doc.get("client_first_name") or ""
        blocks.append(
            f"--- DOCUMENT {index} ---\n"
            f"Type: {doc.get('document_type') or 'QPRT'}\n"
            f"Client: {first} {last}\n"
            f"Pages: {start}-{end}"
        )
        for pn in range(start, end + 1):
            blocks.append(f"--- PAGE {pn} ---\n{(page_texts.get(pn) or '').strip()}")
    return "\n\n".join(blocks)


def extract_qprt_terms(documents, page_texts, total_pages, client, model=None,
                       firm_id=None, filename_fmt=None, today=None):
    """Read each QPRT's initial term and signing date. Does not call the model
    when no QPRT has readable text.

    Returns a list of result dicts. Calculated rows include ``ics`` bytes.
    """
    from ai_logger import log_ai_call, extract_xai_usage, completion_details

    today = today or datetime.now(_EASTERN).date()
    documents = documents or []
    page_texts = page_texts or {}
    total_pages = int(total_pages or 0)

    selected = []
    results_by_slot = {}
    for doc in documents:
        if not isinstance(doc, dict) or not _is_qprt(doc):
            continue
        filename = _build_filename(doc, fmt=filename_fmt)
        span = _span(doc, total_pages)
        if span is None or not _has_page_text(page_texts, span[0], span[1]):
            results_by_slot[len(selected)] = _error_row(filename, _NO_TEXT)
            selected.append(None)
            continue
        selected.append((doc, span[0], span[1], filename))

    readable = [(i, entry) for i, entry in enumerate(selected) if entry is not None]
    if not readable:
        return [results_by_slot[i] for i in range(len(selected))]

    model = model or os.environ.get("DOC_SEPARATOR_MODEL", "gpt-5.6-terra")
    user_message = _qprt_user_message(
        [(doc, start, end) for _, (doc, start, end, _) in readable],
        page_texts,
    )
    call_start = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": QPRT_TERM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )
    except Exception:
        log_ai_call(
            provider="openai", model=model, tool="doc_separator_qprt", status="error",
            execution_ms=int((time.time() - call_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        raise
    call_elapsed = time.time() - call_start
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        parsed = _extract_json(raw)
    except Exception as e:
        log_ai_call(
            provider="openai", model=model, tool="doc_separator_qprt", status="error",
            execution_ms=int(call_elapsed * 1000),
            notes=f"JSON parse failed\n{traceback.format_exc()}",
            firm_id=firm_id,
            **extract_xai_usage(resp),
        )
        e.details = completion_details(resp, raw)
        raise

    log_ai_call(
        provider="openai", model=model, tool="doc_separator_qprt", status="success",
        execution_ms=int(call_elapsed * 1000),
        firm_id=firm_id,
        **extract_xai_usage(resp),
    )

    items = parsed.get("qprts") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        err = ValueError("QPRT response did not include a qprts list.")
        err.details = completion_details(resp, raw)
        raise err

    items = [item for item in items if isinstance(item, dict)]
    by_index = {}
    for item in items:
        index = _as_int(item.get("document_index"))
        if index is None or index in by_index:
            continue
        by_index[index] = item
    expected = set(range(len(readable)))
    if set(by_index) != expected and len(items) == len(readable):
        by_index = dict(enumerate(items))

    results = []
    readable_pos = {slot: pos for pos, (slot, _) in enumerate(readable)}
    for slot, entry in enumerate(selected):
        if entry is None:
            results.append(results_by_slot[slot])
            continue
        doc, start, end, filename = entry
        item = by_index.get(readable_pos[slot])
        if item is None:
            results.append(_error_row(filename, _NO_ANSWER))
            continue
        results.append(_row_from_item(item, doc, filename, (start, end), today))

    log.info("QPRT terms: %d document(s)", len(results))
    return results
