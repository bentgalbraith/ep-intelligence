"""Client-facing summary of uploaded Word documents and PDFs."""

import io
import re
import time
import traceback

from pypdf import PdfReader

TOOL_KEY = "client_doc_summary"
OCR_TOOL_KEY = "client_doc_summary_ocr"
PART_TOOL_KEY = "client_doc_summary_part"
REDO_TOOL_KEY = "client_doc_summary_redo"

# A page with at least this much extracted text has a real text layer, not a scan.
_TEXT_PAGE_CHARS = 40
# Stay inside one model call. Longer uploads are summarized in batches, then combined.
_MAX_SOURCE_CHARS = 100_000
_MAX_BATCHES = 8

_CLIENT_PROMPT = """\
You are a legal assistant at an estate planning firm.{firm_context}

You will receive the text of one or more estate planning documents. Write a summary \
the firm can send to the client.

Write a very concise summary in plain paragraphs. Use simple, everyday language. \
No headings, bullets, numbering, or markdown.

Write every sentence to the people the documents are for, in the second person \
("you" and "your"). The documents belong to them, so "your trust" rather than \
"John's trust" or "the client's trust." Never call them "the client," "the grantor," \
"the trustor," or "he," "she," or "they." Anyone who is not one of those people \
keeps their name ("your trustee, Alex Kim").

When the documents are for one person, "you" is that person. A spouse who is only \
mentioned in the documents keeps their name ("your spouse, Jane Doe").

When the documents are for a couple, such as two spouses who are both grantors or \
both testators, "you" means both of them in every sentence. Address them together. \
Do not pick one spouse as "you" and leave the other in the third person, and do not \
open by naming one of them. Write "You created a trust together," not "John, this \
is your trust with Jane Doe," and not "John and Jane Doe created a trust." Use a \
name only to tell their documents apart: "You each signed a will. John's will \
leaves the house to Jane, and Jane's will leaves the house to John."

Cover three things, and little else: what each document is at a high level; who \
the people are, including beneficiaries and anyone else in a role such as trustee \
or agent; and where assets go.

Rules:
- Use only facts stated in the documents, or in additional details the firm \
includes with them. Do not guess or invent names, dates, amounts, roles, or \
legal effects.
- Synthesize every document into one summary. Do not write a separate summary per \
file unless the documents concern unrelated people or plans and combining them \
would mix those up. A couple's documents stay in that one summary, addressed to both.
- Err on the side of brevity. A few short paragraphs is enough. Leave out \
boilerplate, definitions, and anything a client does not need.
- Describe what the documents provide. Do not recommend changes or give legal advice.
- If something important is unclear or missing from the text, say so in one sentence.
"""

_NOTES_PROMPT = """\
You are a legal assistant at an estate planning firm.{firm_context}

You will receive part of a larger set of estate planning documents. List only the \
facts a client would need from this part: what each document is at a high level, \
who the people are (beneficiaries and anyone else in a role), and where assets go. \
Plain sentences only. Be brief. No headings, bullets, or advice. When a fact \
is about the person the documents are for, say "the client" so a later pass can \
address them as "you." When the documents are for a couple, say "the clients" for \
facts about both of them. Do not treat one spouse as the client and the other as \
someone else. Use only facts stated in the text or in additional details the firm \
includes. Do not invent anything.
"""

_COMBINE_PROMPT = """\
You are a legal assistant at an estate planning firm.{firm_context}

You will receive notes taken from a client's estate planning documents. Turn them \
into one very concise summary the firm can send to the client.

Write plain paragraphs in simple, everyday language. No headings, bullets, \
numbering, or markdown. The notes may describe the clients in the third person. \
Rewrite every sentence as "you" and "your." Never write "the client," \
"the clients," "the grantor," "the trustor," or "he," "she," or "they" for the \
people the documents are for. If the notes are about a couple, "you" means both \
of them. Do not address only one spouse, and do not leave the other in the third \
person. Write "You created a trust together," not "John, this is your trust with \
Jane." Use a name only to tell their documents apart. Other people keep their \
names. Cover what each document \
is at a high level, who the people are (beneficiaries and anyone else in a role), \
and where assets go. Err on the side of brevity. A few short paragraphs is enough. \
Leave out boilerplate and anything a client does not need. Use only the notes and \
any additional details the firm includes. Do not invent facts, recommend changes, \
or give legal advice.
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


def _with_user_details(user, details):
    details = (details or "").strip()
    if not details:
        return user
    return (
        f"{user}\n\n--- ADDITIONAL DETAILS FROM THE FIRM ---\n{details}"
    )


def summarize_documents(uploads, client, model, firm_id=None, firm_config=None, details=""):
    """Read each upload and return (summary, documents).

    uploads: list of (filename, bytes).
    details: optional notes the user typed for the model to consider.
    documents is the extracted text, kept so a later redo can skip re-reading the files.
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
        summary = _complete(client, model, client_prompt, _with_user_details(user, details), firm_id)
        return summary, documents

    notes_prompt = _with_firm_context(_NOTES_PROMPT, firm_config)
    notes = []
    for batch in batches:
        user = "\n\n".join(_source_block(name, text) for name, text in batch)
        notes.append(_complete(
            client, model, notes_prompt, _with_user_details(user, details), firm_id, tool=PART_TOOL_KEY,
        ))
    combined = "\n\n".join(f"--- Notes {index} ---\n{note}" for index, note in enumerate(notes, 1))
    summary = _complete(
        client,
        model,
        _with_firm_context(_COMBINE_PROMPT, firm_config),
        _with_user_details(combined, details),
        firm_id,
    )
    return summary, documents


def _redo_system(firm_config, previous_summary, feedback):
    """Same client-summary rules, plus the firm's correction."""
    base = _with_firm_context(_CLIENT_PROMPT, firm_config)
    return (
        f"{base}\n\n"
        "You already wrote this summary for the client:\n"
        f"{previous_summary.strip()}\n\n"
        "The firm reviewed it and asked for this change:\n"
        f"{feedback.strip()}\n\n"
        "Rewrite the summary. Apply that feedback, and keep everything else that was correct. "
        "Write it to the clients as \"you\" and \"your,\" even if the summary above does not. "
        "If the documents are for a couple, \"you\" means both of them. "
        "Do not keep a draft that addresses only one spouse. "
        "Treat a fact stated in the feedback as something to include. Do not invent anything "
        "beyond the documents, the firm's additional details, and that feedback."
    )


def redo_summary(documents, previous_summary, feedback, client, model,
                 firm_id=None, firm_config=None, details=""):
    """Rewrite a finished summary from the same document text and the firm's feedback."""
    feedback = (feedback or "").strip()
    previous_summary = (previous_summary or "").strip()
    if not documents:
        raise SummaryError("That summary expired. Upload the documents and summarize again.")
    if not previous_summary:
        raise SummaryError("There is no summary to redo.")
    if not feedback:
        raise SummaryError("Describe what to change.")

    system = _redo_system(firm_config, previous_summary, feedback)
    batches = _batches(documents)
    if len(batches) == 1:
        user = "\n\n".join(_source_block(name, text) for name, text in batches[0])
        return _complete(client, model, system, _with_user_details(user, details), firm_id, tool=REDO_TOOL_KEY)

    notes_prompt = _with_firm_context(_NOTES_PROMPT, firm_config)
    notes = []
    for batch in batches:
        user = "\n\n".join(_source_block(name, text) for name, text in batch)
        notes.append(_complete(
            client, model, notes_prompt, _with_user_details(user, details), firm_id, tool=PART_TOOL_KEY,
        ))
    combined = "\n\n".join(f"--- Notes {index} ---\n{note}" for index, note in enumerate(notes, 1))
    return _complete(
        client, model, system, _with_user_details(combined, details), firm_id, tool=REDO_TOOL_KEY,
    )
