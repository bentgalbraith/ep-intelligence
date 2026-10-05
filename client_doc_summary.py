"""Client-facing summary of uploaded Word documents and PDFs."""

import io
import re
import time
import traceback

from pypdf import PdfReader

TOOL_KEY = "client_doc_summary"
OCR_TOOL_KEY = "client_doc_summary_ocr"
PART_TOOL_KEY = "client_doc_summary_part"

# A page with at least this much extracted text has a real text layer, not a scan.
_TEXT_PAGE_CHARS = 40
# Stay inside one model call. Longer uploads are summarized in batches, then combined.
_MAX_SOURCE_CHARS = 100_000
_MAX_BATCHES = 8

_CLIENT_PROMPT = """\
You are a legal assistant at an estate planning firm.{firm_context}

You will receive the text of one or more estate planning documents. Write a summary \
the firm can send to the client.

Write a very concise summary in plain paragraphs. Cover what the documents are and \
the key details the client should be aware of. Use simple, everyday language. \
Address the client as "you" and "your." No headings, bullets, numbering, or markdown.

Rules:
- Use only facts stated in the documents. Do not guess or invent names, dates, \
amounts, roles, or legal effects.
- Synthesize every document into one summary. Do not write a separate summary per \
file unless the documents concern different people or plans and combining them \
would mix those up.
- Keep it short. A few short paragraphs is enough.
- Describe what the documents provide. Do not recommend changes or give legal advice.
- If something important is unclear or missing from the text, say so in one sentence.
"""

_NOTES_PROMPT = """\
You are a legal assistant at an estate planning firm.{firm_context}

You will receive part of a larger set of estate planning documents. List the facts \
a client would need from this part: what each document is, who the people are, \
and what the document says happens. Plain sentences only. No headings, bullets, \
or advice. Use only facts stated in the text. Do not invent anything.
"""

_COMBINE_PROMPT = """\
You are a legal assistant at an estate planning firm.{firm_context}

You will receive notes taken from a client's estate planning documents. Turn them \
into one very concise summary the firm can send to the client.

Write plain paragraphs in simple, everyday language. Address the client as "you" \
and "your." No headings, bullets, numbering, or markdown. Cover what the documents \
are and the key details the client should be aware of. A few short paragraphs is \
enough. Use only the notes. Do not invent facts, recommend changes, or give legal advice.
"""


class SummaryError(Exception):
    """A problem the user can act on.

    notify=True means an operator should hear about it (model or scan service),
    not a file the user can fix.
    """

    def __init__(self, message, notify=False):
        super().__init__(message)
        self.notify = notify


def _with_firm_context(template, firm_config):
    ctx = ((firm_config or {}).get("firm_context") or "").strip()
    if ctx:
        ctx = " " + ctx
    return template.replace("{firm_context}", ctx)


def to_paragraphs(raw):
    """Collapse model output into blank-line-separated paragraphs."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = []
    buf = []

    def flush():
        paragraph = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if paragraph:
            parts.append(paragraph)
        buf.clear()

    for line in text.split("\n"):
        original = line.strip().replace("**", "").replace("__", "")
        cleaned = re.sub(r"^#{1,6}\s*", "", original)
        cleaned = re.sub(r"^[-*•]\s+", "", cleaned)
        cleaned = re.sub(r"^\d{1,2}[.)]\s+", "", cleaned)
        if not original:
            flush()
            continue
        # Headings and list lines become their own paragraphs. Wrapped prose stays together.
        if cleaned != original:
            flush()
            if cleaned:
                parts.append(re.sub(r"\s+", " ", cleaned).strip())
            continue
        buf.append(cleaned)
    flush()
    if not parts:
        raise SummaryError("The summary came back empty. Try again.")
    return "\n\n".join(parts)


def _join_pages(pages):
    chunks = []
    for number, text in enumerate(pages, 1):
        body = (text or "").strip()
        if body:
            chunks.append(f"Page {number}\n{body}")
    return "\n\n".join(chunks).strip()


def _pdf_reader(data, name):
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise SummaryError(f"Could not read '{name}'. It may not be a valid PDF.") from exc
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:
            raise SummaryError(
                f"'{name}' is password-protected. Remove the password and upload it again."
            ) from exc
        if unlocked == 0:
            raise SummaryError(
                f"'{name}' is password-protected. Remove the password and upload it again."
            )
    return reader


def _text_layer(reader):
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(text.strip())
    return pages


def _weak_page_indexes(pages):
    """Pages with too little text to trust. Those are scanned or blank."""
    return [i for i, text in enumerate(pages) if len((text or "").strip()) < _TEXT_PAGE_CHARS]


def _ocr_pages(reader, indexes, name, firm_id):
    """Scan only the given 0-based page indexes. Returns {index: text}."""
    from ai_logger import log_ai_call
    from doc_separator import PAGES_PER_CHUNK, _get_documentai_client, _page_text
    from google.cloud import documentai_v1 as documentai
    from pypdf import PdfWriter

    if not indexes:
        return {}

    started = time.time()
    try:
        client, processor_name = _get_documentai_client()
    except Exception as exc:
        raise SummaryError(f"Could not read the scan in '{name}'.", notify=True) from exc

    texts = {}
    try:
        for start in range(0, len(indexes), PAGES_PER_CHUNK):
            chunk = indexes[start:start + PAGES_PER_CHUNK]
            writer = PdfWriter()
            for index in chunk:
                writer.add_page(reader.pages[index])
            buf = io.BytesIO()
            writer.write(buf)
            result = client.process_document(
                request=documentai.ProcessRequest(
                    name=processor_name,
                    raw_document=documentai.RawDocument(
                        content=buf.getvalue(),
                        mime_type="application/pdf",
                    ),
                )
            )
            for local_idx, page in enumerate(result.document.pages):
                if local_idx < len(chunk):
                    texts[chunk[local_idx]] = _page_text(result.document, page)
    except SummaryError:
        raise
    except Exception as exc:
        log_ai_call(
            provider="google_documentai",
            tool=OCR_TOOL_KEY,
            status="error",
            pages_processed=len(indexes),
            execution_ms=int((time.time() - started) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        raise SummaryError(f"Could not read the scan in '{name}'.", notify=True) from exc

    log_ai_call(
        provider="google_documentai",
        tool=OCR_TOOL_KEY,
        status="success",
        pages_processed=len(indexes),
        execution_ms=int((time.time() - started) * 1000),
        firm_id=firm_id,
    )
    return texts


def read_pdf(data, name, firm_id=None):
    """Keep a real text layer. Scan only the pages that do not have one."""
    reader = _pdf_reader(data, name)
    if len(reader.pages) == 0:
        raise SummaryError(f"'{name}' has no pages.")
    pages = _text_layer(reader)
    weak = _weak_page_indexes(pages)
    if weak:
        scanned = _ocr_pages(reader, weak, name, firm_id)
        for index, text in scanned.items():
            if (text or "").strip():
                pages[index] = text.strip()
    return _join_pages(pages)


def read_docx(data, name):
    from compare_diagram_drafts import CompareError, extract_docx_text

    try:
        return extract_docx_text(data)
    except CompareError as exc:
        raise SummaryError(f"Could not read '{name}'. Upload a .docx file.") from exc


def read_upload(name, data, firm_id=None):
    filename = (name or "document").strip() or "document"
    lower = filename.lower()
    if lower.endswith(".doc") and not lower.endswith(".docx"):
        raise SummaryError(
            f"'{filename}' is an older Word file. Save it as .docx and upload it again."
        )
    if lower.endswith(".pdf"):
        text = read_pdf(data, filename, firm_id=firm_id)
    elif lower.endswith(".docx"):
        text = read_docx(data, filename)
    else:
        raise SummaryError(f"'{filename}' is not a Word document (.docx) or a PDF.")
    if not (text or "").strip():
        raise SummaryError(f"No text could be read from '{filename}'.")
    return filename, text.strip()


def _source_block(name, text):
    return f"--- {name} ---\n{text.strip()}"


def _split_text(text, limit):
    if len(text) <= limit:
        return [text]
    parts = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            break_at = text.rfind("\n\n", start, end)
            if break_at > start + limit // 2:
                end = break_at
        piece = text[start:end].strip()
        if piece:
            parts.append(piece)
        start = end if end > start else start + limit
    return parts


def _batches(documents):
    batches = []
    current = []
    size = 0
    for name, text in documents:
        blocks = _split_text(text, max(_MAX_SOURCE_CHARS - len(name) - 16, 1000))
        for index, piece in enumerate(blocks):
            label = name if len(blocks) == 1 else f"{name} (part {index + 1})"
            block = _source_block(label, piece)
            if current and size + len(block) > _MAX_SOURCE_CHARS:
                batches.append(current)
                current = []
                size = 0
            current.append((label, piece))
            size += len(block)
    if current:
        batches.append(current)
    if len(batches) > _MAX_BATCHES:
        raise SummaryError(
            "These documents are too long to summarize together. Remove a file and try again."
        )
    return batches


def _complete(client, model, system, user, firm_id, tool=TOOL_KEY):
    from ai_logger import extract_xai_usage, log_ai_call

    started = time.time()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as exc:
        log_ai_call(
            provider="openai",
            model=model,
            tool=tool,
            status="error",
            execution_ms=int((time.time() - started) * 1000),
            notes=traceback.format_exc(),
            firm_id=firm_id,
        )
        raise SummaryError("The summary could not be completed. Try again.", notify=True) from exc

    choice = response.choices[0] if response.choices else None
    raw = (choice.message.content or "").strip() if choice and choice.message else ""
    elapsed_ms = int((time.time() - started) * 1000)
    try:
        summary = to_paragraphs(raw)
    except SummaryError:
        log_ai_call(
            provider="openai",
            model=model,
            tool=tool,
            status="error",
            execution_ms=elapsed_ms,
            notes="Model returned an empty summary.",
            firm_id=firm_id,
            **extract_xai_usage(response),
        )
        raise SummaryError("The summary came back empty. Try again.", notify=True)
    log_ai_call(
        provider="openai",
        model=model,
        tool=tool,
        status="success",
        execution_ms=elapsed_ms,
        firm_id=firm_id,
        **extract_xai_usage(response),
    )
    return summary


def _disambiguate_names(uploads):
    """Give duplicate filenames distinct labels so the summary can tell them apart."""
    totals = {}
    for name, _data in uploads:
        key = name.lower()
        totals[key] = totals.get(key, 0) + 1
    seen = {}
    labeled = []
    for name, data in uploads:
        key = name.lower()
        if totals[key] == 1:
            labeled.append((name, data))
            continue
        seen[key] = seen.get(key, 0) + 1
        stem, dot, ext = name.rpartition(".")
        label = f"{stem} ({seen[key]}).{ext}" if dot else f"{name} ({seen[key]})"
        labeled.append((label, data))
    return labeled


def summarize_documents(uploads, client, model, firm_id=None, firm_config=None):
    """Read each upload and return a plain-paragraph client summary.

    uploads: list of (filename, bytes).
    """
    if not uploads:
        raise SummaryError("Upload at least one Word document or PDF.")

    documents = [
        read_upload(name, data, firm_id=firm_id)
        for name, data in _disambiguate_names(uploads)
    ]
    batches = _batches(documents)
    client_prompt = _with_firm_context(_CLIENT_PROMPT, firm_config)

    if len(batches) == 1:
        user = "\n\n".join(_source_block(name, text) for name, text in batches[0])
        return _complete(client, model, client_prompt, user, firm_id)

    notes_prompt = _with_firm_context(_NOTES_PROMPT, firm_config)
    notes = []
    for batch in batches:
        user = "\n\n".join(_source_block(name, text) for name, text in batch)
        notes.append(_complete(client, model, notes_prompt, user, firm_id, tool=PART_TOOL_KEY))
    combined = "\n\n".join(f"--- Notes {index} ---\n{note}" for index, note in enumerate(notes, 1))
    return _complete(
        client,
        model,
        _with_firm_context(_COMBINE_PROMPT, firm_config),
        combined,
        firm_id,
    )
