"""PDF document separation: OCR via Google Document AI, boundary detection via Grok."""

import io
import json
import logging
import os
import re
import time
import traceback
import zipfile

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

Identify every individual document within the scan. For each document provide:
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
- Include cover sheets, TOCs, exhibit lists, exhibits, schedules, and addenda \
with their parent document. They are not standalone documents.
- Capitalize names properly.
- Pages with scanned ID cards (which may be hard to identify via OCR) must not \
be skipped. Name them using standard conventions with a specific document_type \
like "Florida ID" or "Indiana Driver's License".
- Uploads often contain documents for both a husband and a wife. Many documents \
are specific to only one person — do not merge distinct documents pertaining to \
two different people into one entry.
- A HIPAA authorization (e.g. "Authorization for Release of Protected Health \
Information") is always its own standalone document, never part of a living will \
or advance directive.
- A Certification of Trust (which often states that relevant portions of the trust \
agreement are attached) plus any attached trust excerpts together form one \
document — the Certification of Trust.
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


def _identify_documents(xai_client, page_texts, total_pages, model, firm_id=None, extra_rules=""):
    """Ask Grok to identify document boundaries and metadata."""
    from ai_logger import log_ai_call, extract_xai_usage

    pages_block = ""
    for pn in range(1, total_pages + 1):
        txt = page_texts.get(pn, "").strip()
        pages_block += f"\n--- PAGE {pn} ---\n{txt}\n"

    system_prompt = DOC_SEPARATOR_PROMPT
    if extra_rules:
        system_prompt += f"\n\nAdditional rules from the firm:\n{extra_rules}"

    grok_start = time.time()
    try:
        resp = xai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": pages_block},
            ],
            temperature=0,
            max_tokens=524288,
        )
    except Exception:
        log_ai_call(
            provider="xai", model=model, tool="doc_separator", status="error",
            execution_ms=int((time.time() - grok_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        raise
    grok_elapsed = time.time() - grok_start

    log_ai_call(
        provider="xai", model=model, tool="doc_separator", status="success",
        execution_ms=int(grok_elapsed * 1000),
        firm_id=firm_id,
        **extract_xai_usage(resp),
    )

    usage = resp.usage
    finish_reason = resp.choices[0].finish_reason
    log.info(
        "Grok: %.1fs, %s/%s tokens (prompt/completion), finish_reason=%s",
        grok_elapsed,
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

    parsed = _extract_json(raw)

    if "documents" in parsed:
        docs = parsed["documents"]
        log.info("Parsed %d documents", len(docs))
        return docs
    if isinstance(parsed, list):
        log.info("Parsed %d documents", len(parsed))
        return parsed

    raise ValueError(
        f"Unexpected JSON structure (keys: {list(parsed.keys())}): {raw[:500]}"
    )


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


def separate_documents(pdf_content, xai_client, model=None, firm_id=None, firm_config=None):
    """OCR -> detect boundaries -> split -> zip.

    Returns (BytesIO zip, doc list, page_texts, total_pages).
    """
    total_start = time.time()
    model = model or os.environ.get(
        "XAI_DOC_SEPARATOR_MODEL", "grok-4-1-fast-non-reasoning"
    )
    firm_config = firm_config or {}
    log.info("Starting: model=%s, PDF=%d bytes", model, len(pdf_content))

    page_texts, total_pages = _ocr_pages(pdf_content, firm_id=firm_id)
    documents = _identify_documents(
        xai_client, page_texts, total_pages, model,
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


def redo_with_feedback(pdf_content, xai_client, page_texts, total_pages,
                       previous_documents, feedback, model=None, firm_id=None, firm_config=None):
    """Re-run boundary detection with user feedback, skipping OCR.

    Returns (BytesIO zip, doc list).
    """
    from ai_logger import log_ai_call, extract_xai_usage

    total_start = time.time()
    model = model or os.environ.get(
        "XAI_DOC_SEPARATOR_MODEL", "grok-4-1-fast-non-reasoning"
    )
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

    grok_start = time.time()
    try:
        resp = xai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": pages_block},
            ],
            temperature=0,
            max_tokens=524288,
        )
    except Exception:
        log_ai_call(
            provider="xai", model=model, tool="doc_separator_redo", status="error",
            execution_ms=int((time.time() - grok_start) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        raise
    grok_elapsed = time.time() - grok_start

    log_ai_call(
        provider="xai", model=model, tool="doc_separator_redo", status="success",
        execution_ms=int(grok_elapsed * 1000),
        firm_id=firm_id,
        **extract_xai_usage(resp),
    )

    usage = resp.usage
    finish_reason = resp.choices[0].finish_reason
    log.info(
        "Redo Grok: %.1fs, %s/%s tokens (prompt/completion), finish_reason=%s",
        grok_elapsed,
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

    parsed = _extract_json(raw)
    if "documents" in parsed:
        documents = parsed["documents"]
    elif isinstance(parsed, list):
        documents = parsed
    else:
        raise ValueError(
            f"Unexpected JSON structure (keys: {list(parsed.keys())}): {raw[:500]}"
        )

    log.info("Redo parsed %d documents", len(documents))
    filename_fmt = firm_config.get("doc_filename_format")
    zip_buf = _split_and_zip(pdf_content, documents, filename_fmt=filename_fmt)
    total_elapsed = time.time() - total_start
    log.info("Redo complete: %d docs, %.1fs total", len(documents), total_elapsed)
    return zip_buf, documents
