"""Find signature, initial, and notary lines on uploaded PDFs.

Gemini reads each PDF in page chunks. A rules pass cleans the list. GPT checks
only the pages that are still unclear. Crops are cut from the original PDF.
"""

import base64
import json
import logging
import os
import time
import urllib.request
import uuid

log = logging.getLogger("validate_signatures")

TOOL_KEY = "validate_signatures"
SCAN_TOOL_KEY = "validate_signatures_scan"
VERIFY_TOOL_KEY = "validate_signatures_verify"
GEMINI_MODEL = "gemini-3.8-flash"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL
    + ":generateContent"
)
VERIFY_MODEL = "gpt-6-sol"

MAX_FILES = 20
CHUNK_PAGES = 15
# Raw PDF bytes. Base64 adds about a third, and the inline request limit is 20MB.
CHUNK_BYTES = 12 * 1024 * 1024
MAX_ITEMS_PER_CHUNK = 400
_TYPES = ("signature", "initial", "notary")
_STATUSES = ("completed", "blank", "ambiguous")
_DATE_LINES = ("filled", "blank", "none", "unclear")
_CONFIDENCE = {"high": 3, "medium": 2, "low": 1}

_NOT_CONFIGURED = "Name Files & Validate Signatures is not configured yet. Contact your administrator."
_CHECK_FAILED = "A document could not be checked. Please try again."

_SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(_TYPES)},
                    "status": {"type": "string", "enum": list(_STATUSES)},
                    "page": {"type": "integer"},
                    "box": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": (
                            "x, y, width, height as fractions of the page from 0 to 1. "
                            "x and y are the top-left corner. Width extends right and "
                            "height extends down. Not a bottom-right corner pair."
                        ),
                    },
                    "label": {"type": "string"},
                    "date_line": {"type": "string", "enum": list(_DATE_LINES)},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": [
                    "type", "status", "page", "box", "label", "date_line", "confidence",
                ],
            },
        },
    },
    "required": ["items"],
}

_SCAN_PROMPT = """You are checking an estate-planning PDF for every place a person is supposed to sign, initial, or notarize.

This file has {count} pages. Number those pages from 1. Page 1 is the first page of this file, not a page number printed in a footer.

Find every signature line, initial line, and notarization block, whether it is filled in or blank. Include signature blocks, initial lines in the margins and footers, witness lines, notary acknowledgments, jurats, and notary seals.

Do not list ordinary sentences, dates on their own, or blank lines that are not for a signature, initial, or notarization.

For each item:
- type is signature, initial, or notary
- status is completed when ink, a stamp, or a seal is present; blank when the line or block is empty; ambiguous when you cannot tell
- page is an integer from 1 through {count}
- box is [x, y, width, height], fractions of the full page from 0 to 1, origin at the top-left. Box the handwriting or stamp and the line it sits on, plus the printed name under that line. Do not box the paragraph above the line, and do not box a single letter
- label is a few words such as "Grantor", "Witness", or "Notary acknowledgment". Do not quote the document
- date_line is filled when a date beside this item is written in, blank when that date line is empty, none when there is no date line, unclear when you cannot tell
- confidence is high, medium, or low

Return every item. An empty list is correct only when this part has nowhere to sign, initial, or notarize.
"""

_VERIFY_PROMPT = """This image is document page {page} of an estate-planning PDF.

Another model found the items below on this page. Confirm each status. You may add a signature, initial, or notarization it missed. Do not remove an item that is really on the page.

Items:
{items}

Return JSON with an "items" array. For an item already listed, copy its id. For a new item, omit id. Each item has:
- type: signature, initial, or notary
- status: completed when ink, a stamp, or a seal is visible; blank when the line or block is empty; ambiguous when you cannot tell
- box: [x, y, width, height], fractions of this page from 0 to 1, origin at the top-left
- label: a few words, with no quotation from the document
- date_line: filled, blank, none, or unclear
"""


class SignatureError(Exception):
    def __init__(self, message, *, logged=False, notify=False, alert=None, notes=None):
        super().__init__(message)
        self.logged = logged
        self.notify = notify
        self.alert = alert or message
        self.notes = notes or message


def safe_name(name):
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        base = "document.pdf"
    if len(base) > 180:
        stem, dot, ext = base.rpartition(".")
        if dot and ext.lower() == "pdf" and stem:
            base = stem[: 180 - len(ext) - 1].rstrip() + "." + ext
        else:
            base = base[:180]
    return base


def disambiguate_names(names):
    totals = {}
    for name in names:
        totals[name] = totals.get(name, 0) + 1
    seen = {}
    out = []
    for name in names:
        if totals[name] == 1:
            out.append(name)
            continue
        seen[name] = seen.get(name, 0) + 1
        out.append(f"{name} ({seen[name]})")
    return out


def prepare_uploads(files):
    """Return [(display name, pdf bytes)] or raise SignatureError for a bad upload."""
    if not files:
        raise SignatureError("Upload at least one PDF.")
    if len(files) > MAX_FILES:
        raise SignatureError(f"Upload up to {MAX_FILES} PDFs at a time.")
    ready = []
    for name, data in files:
        display = safe_name(name)
        if not display.lower().endswith(".pdf"):
            raise SignatureError(f"'{display}' is not a PDF.")
        if not data:
            raise SignatureError(f"'{display}' is empty.")
        if b"%PDF" not in data[:1024]:
            raise SignatureError(f"'{display}' is not a PDF.")
        ready.append((display, data))
    names = disambiguate_names([name for name, _ in ready])
    return [(names[i], ready[i][1]) for i in range(len(ready))]


def chunk_spans(page_count, too_big, page_limit=CHUNK_PAGES):
    """Inclusive 0-based page spans. too_big(start, end) splits a span in half."""
    if page_count < 1:
        return []
    spans = []
    start = 0
    while start < page_count:
        end = min(start + page_limit, page_count) - 1
        while too_big(start, end) and end > start:
            end = start + (end - start + 1) // 2 - 1
            if end < start:
                end = start
                break
        if too_big(start, end):
            raise SignatureError("A page in this PDF is too large to check.")
        spans.append((start, end))
        start = end + 1
    return spans


def parse_box(raw):
    if isinstance(raw, dict):
        raw = [raw.get("x"), raw.get("y"), raw.get("width", raw.get("w")), raw.get("height", raw.get("h"))]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        nums = [float(n) for n in raw]
    except (TypeError, ValueError):
        return None
    if any(n != n for n in nums):
        return None
    if max(abs(n) for n in nums) > 1.5:
        nums = [n / 1000.0 for n in nums]
    # Gemini often emits [ymin, xmin, ymax, xmax]. Use that only when the
    # requested x, y, width, height box runs off the page and the corner
    # form lands on the page.
    if not _runs_off_page(*nums):
        return _clamp_box(*nums)
    y0, x0, y1, x1 = nums
    if not _runs_off_page(x0, y0, x1 - x0, y1 - y0):
        return _clamp_box(x0, y0, x1 - x0, y1 - y0)
    return _clamp_box(*nums)


def _runs_off_page(x, y, w, h):
    return x < -0.02 or y < -0.02 or w <= 0 or h <= 0 or (x + w) > 1.02 or (y + h) > 1.02


def _clamp_box(x, y, w, h):
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0 - x)
    h = min(max(h, 0.0), 1.0 - y)
    if w <= 0 or h <= 0:
        return None
    if h < 0.02:
        mid = y + h / 2
        h = 0.02
        y = min(max(mid - h / 2, 0.0), 1.0 - h)
    if w < 0.08:
        mid = x + w / 2
        w = 0.08
        x = min(max(mid - w / 2, 0.0), 1.0 - w)
    return (round(x, 4), round(y, 4), round(w, 4), round(h, 4))


def region_label(box):
    x, y, w, h = box
    cx = x + w / 2
    cy = y + h / 2
    if cy < 1 / 3:
        row = "top"
    elif cy > 2 / 3:
        row = "bottom"
    else:
        row = "middle"
    if cx < 1 / 3:
        col = "left"
    elif cx > 2 / 3:
        col = "right"
    else:
        col = ""
    if row == "middle" and not col:
        return "middle"
    if not col:
        return row
    if row == "middle":
        return "middle " + col
    return row + " " + col


def resolve_page(raw_page, start, end):
    """Map a page number within this file (1 = first page of the file) onto the document."""
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return None
    local_count = end - start + 1
    if 1 <= page <= local_count:
        return start + page - 1
    return None


def _enum(value, allowed):
    text = str(value or "").strip().lower()
    if text in allowed:
        return text
    return None


def _short_label(value):
    text = " ".join(str(value or "").split())
    if len(text) > 80:
        text = text[:77].rstrip() + "..."
    return text


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = aw * ah + bw * bh - inter
    if union <= 0:
        return 0.0
    return inter / union


def dedupe_items(items):
    """Drop overlapping boxes of the same type on the same page. Keep the surer one."""
    kept = []
    for item in items:
        replaced = False
        for index, prev in enumerate(kept):
            if prev["file_index"] != item["file_index"] or prev["page"] != item["page"]:
                continue
            if prev["type"] != item["type"]:
                continue
            if _iou(prev["box"], item["box"]) <= 0.5:
                continue
            if _CONFIDENCE.get(item.get("confidence"), 0) > _CONFIDENCE.get(prev.get("confidence"), 0):
                kept[index] = item
            replaced = True
            break
        if not replaced:
            kept.append(item)
    return kept


def mark_uncertain(items):
    for item in items:
        if item.get("confidence") == "low":
            item["status"] = "ambiguous"
        item.pop("confidence", None)
        item["region"] = region_label(item["box"])
    return items


def apply_verification(items, updates):
    """Keep every existing item. An unclear item can become completed or blank.

    A disagreement stays unclear. A new item needs a box. Unknown ids are ignored.
    """
    by_id = {item["id"]: dict(item) for item in items}
    order = [item["id"] for item in items]
    extras = []
    for raw in updates or []:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        status = _enum(raw.get("status"), _STATUSES)
        if item_id:
            current = by_id.get(item_id)
            if current is None or not status:
                continue
            if current["status"] == "ambiguous" and status in ("completed", "blank"):
                current["status"] = status
            elif status != current["status"]:
                current["status"] = "ambiguous"
            date_line = _enum(raw.get("date_line"), _DATE_LINES)
            if date_line and current["date_line"] == "unclear" and date_line != "unclear":
                current["date_line"] = date_line
            label = _short_label(raw.get("label"))
            if label and not current["label"]:
                current["label"] = label
            continue
        kind = _enum(raw.get("type"), _TYPES)
        box = parse_box(raw.get("box"))
        if not kind or not status or not box:
            continue
        sample = items[0] if items else {}
        extras.append({
            "id": uuid.uuid4().hex,
            "file": sample.get("file", ""),
            "file_index": sample.get("file_index", 0),
            "page": sample.get("page", 1),
            "type": kind,
            "status": status,
            "box": box,
            "label": _short_label(raw.get("label")),
            "date_line": _enum(raw.get("date_line"), _DATE_LINES) or "unclear",
            "region": region_label(box),
        })
    return [by_id[item_id] for item_id in order] + extras


def normalize_chunk(raw_items, start, end, name, file_index):
    found = []
    if not isinstance(raw_items, list):
        return found
    if len(raw_items) > MAX_ITEMS_PER_CHUNK:
        raise SignatureError(
            "This document has more signature lines than this check can list. "
            "Split it into smaller PDFs and try again.",
            notify=True,
            alert="Signature item list exceeded cap",
            notes="item cap",
        )
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        kind = _enum(raw.get("type"), _TYPES)
        status = _enum(raw.get("status"), _STATUSES)
        page = resolve_page(raw.get("page"), start, end)
        box = parse_box(raw.get("box"))
        if not kind or not status or page is None or not box:
            continue
        confidence = _enum(raw.get("confidence"), _CONFIDENCE) or "medium"
        found.append({
            "id": uuid.uuid4().hex,
            "file": name,
            "file_index": file_index,
            "page": page,
            "type": kind,
            "status": status,
            "box": box,
            "label": _short_label(raw.get("label")),
            "date_line": _enum(raw.get("date_line"), _DATE_LINES) or "unclear",
            "confidence": confidence,
        })
    return found


def public_item(item):
    return {
        "id": item["id"],
        "file": item["file"],
        "page": item["page"],
        "type": item["type"],
        "status": item["status"],
        "region": item["region"],
        "label": item["label"],
        "date_line": item["date_line"],
    }


def _sort_items(items):
    return sorted(items, key=lambda item: (
        item["file_index"], item["page"], item["box"][1], item["box"][0],
    ))


def _parse_json_object(text):
    """Parse a JSON object. Raises ValueError with no response text attached."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("invalid json")
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            raise ValueError("invalid json")
    if not isinstance(data, dict):
        raise ValueError("invalid json")
    return data


def _gemini_usage(payload):
    usage = (payload or {}).get("usageMetadata") or {}

    def grab(name):
        try:
            return int(usage.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    return grab("promptTokenCount"), grab("candidatesTokenCount"), grab("thoughtsTokenCount")


def _gemini_text(payload):
    candidates = (payload or {}).get("candidates") or []
    if not candidates:
        return "", ""
    finish = candidates[0].get("finishReason") or ""
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    texts = []
    for part in parts:
        if part.get("thought"):
            continue
        value = part.get("text")
        if value:
            texts.append(value)
    return "\n".join(texts), finish


def _log_model(provider, model, tool, status, firm_id, started, input_tokens=0,
               output_tokens=0, reasoning_tokens=0, pages=None, notes=None):
    from ai_logger import log_ai_call
    log_ai_call(
        provider=provider,
        model=model,
        tool=tool,
        status=status,
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
        reasoning_tokens=reasoning_tokens or None,
        pages_processed=pages,
        execution_ms=int((time.time() - started) * 1000),
        notes=notes,
        firm_id=firm_id,
    )


def _gemini_key():
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise SignatureError(
            _NOT_CONFIGURED,
            notify=True,
            alert="GEMINI_API_KEY is not set",
            notes="GEMINI_API_KEY is not set",
        )
    return key


def _post_gemini(pdf_bytes, start, end):
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "inlineData": {
                        "mimeType": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                },
                {"text": _SCAN_PROMPT.format(count=end - start + 1)},
            ],
        }],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "low"},
            "responseMimeType": "application/json",
            "responseJsonSchema": _SCAN_SCHEMA,
            "maxOutputTokens": 32768,
        },
    }
    request = urllib.request.Request(
        _GEMINI_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": _gemini_key(),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def _scan_chunk(pdf_bytes, start, end, firm_id):
    started = time.time()
    payload = None
    pages = end - start + 1
    try:
        payload = _post_gemini(pdf_bytes, start, end)
        raw, finish = _gemini_text(payload)
        prompt, output, thoughts = _gemini_usage(payload)
        if finish == "MAX_TOKENS":
            _log_model(
                "gemini", GEMINI_MODEL, SCAN_TOOL_KEY, "error", firm_id, started,
                input_tokens=prompt, output_tokens=output, reasoning_tokens=thoughts,
                pages=pages, notes="truncated",
            )
            raise SignatureError(
                _CHECK_FAILED, logged=True, notify=True,
                alert="Gemini response truncated", notes="truncated",
            )
        try:
            parsed = _parse_json_object(raw)
        except ValueError:
            _log_model(
                "gemini", GEMINI_MODEL, SCAN_TOOL_KEY, "error", firm_id, started,
                input_tokens=prompt, output_tokens=output, reasoning_tokens=thoughts,
                pages=pages, notes="JSON parse failed",
            )
            raise SignatureError(
                _CHECK_FAILED, logged=True, notify=True,
                alert="Gemini returned invalid JSON", notes="JSON parse failed",
            ) from None
        _log_model(
            "gemini", GEMINI_MODEL, SCAN_TOOL_KEY, "success", firm_id, started,
            input_tokens=prompt, output_tokens=output, reasoning_tokens=thoughts,
            pages=pages, notes=f"pages {start}-{end}",
        )
        items = parsed.get("items")
        return items if isinstance(items, list) else []
    except SignatureError:
        raise
    except Exception as exc:
        prompt, output, thoughts = _gemini_usage(payload)
        status_code = getattr(exc, "code", None)
        log.warning("Signature scan failed: %s status=%s", type(exc).__name__, status_code)
        _log_model(
            "gemini", GEMINI_MODEL, SCAN_TOOL_KEY, "error", firm_id, started,
            input_tokens=prompt, output_tokens=output, reasoning_tokens=thoughts,
            pages=pages, notes=type(exc).__name__,
        )
        raise SignatureError(
            _CHECK_FAILED, logged=True, notify=True, alert="Gemini scan failed", notes=type(exc).__name__,
        ) from None


def _openai_usage(response):
    from ai_logger import extract_xai_usage
    usage = extract_xai_usage(response)
    output = usage.get("output_tokens") or 0
    reasoning = usage.get("reasoning_tokens") or 0
    # completion_tokens already includes reasoning tokens. Log them once each.
    if reasoning and output >= reasoning:
        usage["output_tokens"] = output - reasoning
    return usage


def _verify_page(openai_client, image_bytes, page, items, firm_id):
    started = time.time()
    response = None
    listed = [
        {
            "id": item["id"],
            "type": item["type"],
            "status": item["status"],
            "label": item["label"],
            "date_line": item["date_line"],
        }
        for item in items
    ]
    prompt = (
        _VERIFY_PROMPT
        .replace("{page}", str(int(page)))
        .replace("{items}", json.dumps(listed))
    )
    encoded = base64.b64encode(image_bytes).decode("ascii")
    try:
        response = openai_client.chat.completions.create(
            model=VERIFY_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + encoded,
                        "detail": "high",
                    }},
                ],
            }],
            response_format={"type": "json_object"},
            max_completion_tokens=4000,
        )
        choice = response.choices[0]
        usage = _openai_usage(response)
        if getattr(choice, "finish_reason", None) == "length":
            _log_model(
                "openai", VERIFY_MODEL, VERIFY_TOOL_KEY, "error", firm_id, started,
                notes="truncated", **usage,
            )
            return items
        try:
            parsed = _parse_json_object(choice.message.content or "")
        except ValueError:
            _log_model(
                "openai", VERIFY_MODEL, VERIFY_TOOL_KEY, "error", firm_id, started,
                notes="JSON parse failed", **usage,
            )
            log.warning("Signature verify returned invalid JSON on page %s", page)
            return items
        updates = parsed.get("items")
        if not isinstance(updates, list):
            _log_model(
                "openai", VERIFY_MODEL, VERIFY_TOOL_KEY, "error", firm_id, started,
                notes="missing items", **usage,
            )
            return items
        _log_model(
            "openai", VERIFY_MODEL, VERIFY_TOOL_KEY, "success", firm_id, started,
            notes=f"page {page}", **usage,
        )
        return apply_verification(items, updates)
    except Exception as exc:
        usage = _openai_usage(response) if response is not None else {}
        log.warning("Signature verify failed: %s", type(exc).__name__)
        _log_model(
            "openai", VERIFY_MODEL, VERIFY_TOOL_KEY, "error", firm_id, started,
            notes=type(exc).__name__, **usage,
        )
        return items


def _pymupdf():
    try:
        import pymupdf
    except ImportError:
        raise SignatureError(
            _NOT_CONFIGURED,
            notify=True,
            alert="pymupdf is not installed",
            notes="pymupdf is not installed",
        )
    return pymupdf


def open_pdf(data, name="This PDF"):
    pymupdf = _pymupdf()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise SignatureError(
            f"'{name}' could not be read as a PDF.",
            notes="unreadable pdf",
            alert="A PDF could not be read",
        )
    needs_password = getattr(doc, "needs_pass", False) or (
        getattr(doc, "is_encrypted", False) and doc.page_count < 1
    )
    if needs_password:
        doc.close()
        raise SignatureError(
            f"'{name}' is password-protected. Remove the password and upload it again.",
            notes="password-protected pdf",
            alert="A PDF is password-protected",
        )
    if doc.page_count < 1:
        doc.close()
        raise SignatureError(
            f"'{name}' has no pages.",
            notes="pdf has no pages",
            alert="A PDF has no pages",
        )
    return doc


def slice_pdf(doc, start, end):
    pymupdf = _pymupdf()
    out = pymupdf.open()
    try:
        out.insert_pdf(doc, from_page=start, to_page=end)
        return out.tobytes(deflate=True)
    finally:
        out.close()


def _matrix(zoom):
    pymupdf = _pymupdf()
    return pymupdf.Matrix(zoom, zoom)


def _render_page(page, max_width):
    """Rasterize the page as it is displayed, including rotation."""
    pymupdf = _pymupdf()
    width = max(page.rect.width, 1)
    zoom = min(2.5, max(1.25, max_width / width))
    pix = page.get_pixmap(matrix=_matrix(zoom), alpha=False, colorspace=pymupdf.csRGB)
    if pix.width > max_width:
        zoom = zoom * (max_width / pix.width)
        pix = page.get_pixmap(matrix=_matrix(zoom), alpha=False, colorspace=pymupdf.csRGB)
    return pix


def _inch(page_size, inches):
    return inches * 72.0 / max(float(page_size), 1.0)


def _displayed_rect(page, rect):
    """Map a box from PDF coordinates into the upright page the reader sees."""
    pymupdf = _pymupdf()
    shown = pymupdf.Rect(rect) * page.rotation_matrix
    return pymupdf.Rect(
        min(shown.x0, shown.x1), min(shown.y0, shown.y1),
        max(shown.x0, shown.x1), max(shown.y0, shown.y1),
    )


def _fraction_rect(page, rect):
    shown = _displayed_rect(page, rect)
    bounds = page.rect
    width = max(bounds.width, 1)
    height = max(bounds.height, 1)
    x0 = min(max((shown.x0 - bounds.x0) / width, 0.0), 1.0)
    y0 = min(max((shown.y0 - bounds.y0) / height, 0.0), 1.0)
    x1 = min(max((shown.x1 - bounds.x0) / width, 0.0), 1.0)
    y1 = min(max((shown.y1 - bounds.y0) / height, 0.0), 1.0)
    if x1 - x0 < 0.012 or y1 - y0 < 0.01:
        return None
    return (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))


def _rule_lines(page):
    """Horizontal rules that can be a signature or initial line, in PDF points."""
    width = max(page.mediabox.width, 1)
    height = max(page.mediabox.height, 1)
    lines = []
    for drawing in page.get_drawings():
        for item in drawing.get("items") or []:
            kind = item[0]
            if kind == "l":
                start, end = item[1], item[2]
                if abs(start.y - end.y) > 1.4:
                    continue
                x0, x1 = sorted((start.x, end.x))
                y = (start.y + end.y) / 2.0
            elif kind == "re":
                rect = item[1]
                if rect.height > 2.4 or rect.width < 36:
                    continue
                x0, x1 = rect.x0, rect.x1
                y = (rect.y0 + rect.y1) / 2.0
            else:
                continue
            length = x1 - x0
            if length < 36 or length > width * 0.9:
                continue
            # A long rule in the top band is a running header, not a signature.
            if y < height * 0.16 and length > width * 0.55:
                continue
            lines.append((x0, y, x1))
    lines.sort(key=lambda line: (line[1], line[0]))
    return lines


def _ink_rects(page):
    """Handwriting images and filled marks, in PDF points. Skips a full-page scan."""
    pymupdf = _pymupdf()
    found = []
    try:
        images = page.get_image_info()
    except Exception:
        images = []
    for info in images:
        found.append(pymupdf.Rect(info["bbox"]))
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if not rect or not drawing.get("fill"):
            continue
        found.append(pymupdf.Rect(rect))
    marks = []
    for rect in found:
        if 16 <= rect.width <= 440 and 8 <= rect.height <= 90:
            marks.append(rect)
    return marks


def _cluster_rules(lines):
    """Join a signature rule with the date rule on the same baseline."""
    rows = []
    for x0, y, x1 in lines:
        placed = False
        for row in rows:
            if abs(row["y"] - y) > 2.6:
                continue
            gap = x0 - row["x1"] if x0 >= row["x0"] else row["x0"] - x1
            if gap < 56:
                row["x0"] = min(row["x0"], x0)
                row["x1"] = max(row["x1"], x1)
                row["y"] = (row["y"] + y) / 2.0
                placed = True
                break
        if not placed:
            rows.append({"x0": x0, "x1": x1, "y": y})
    return rows


def _words_under(words, x0, x1, y):
    return [
        word for word in words
        if y - 1 <= word[1] <= y + 16 and word[2] > x0 - 8 and word[0] < x1 + 8
    ]


def _words_left_of_short_rule(words, x0, y):
    """The label on the same row as a short line, stopping at a wide gap."""
    nearby = [
        word for word in words
        if word[2] <= x0 + 4 and abs(((word[1] + word[3]) / 2.0) - y) <= 9
    ]
    nearby.sort(key=lambda word: word[0])
    chain = []
    edge = x0
    for word in reversed(nearby):
        if edge - word[2] > 36:
            break
        chain.append(word)
        edge = word[0]
    chain.reverse()
    if not chain:
        return []
    if x0 - chain[0][0] <= 300:
        return chain
    kept = []
    for word in reversed(chain):
        if x0 - word[0] > 160:
            break
        kept.append(word)
    kept.reverse()
    return kept


def _union(rects, fallback):
    pymupdf = _pymupdf()
    if not rects:
        return pymupdf.Rect(fallback)
    x0 = min(rect[0] for rect in rects)
    y0 = min(rect[1] for rect in rects)
    x1 = max(rect[2] for rect in rects)
    y1 = max(rect[3] for rect in rects)
    return pymupdf.Rect(x0, y0, x1, y1)


def _stamp_rects(page):
    """Notary seals drawn as circles. Concentric rings become one stamp."""
    pymupdf = _pymupdf()
    found = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        items = drawing.get("items") or []
        kinds = [item[0] for item in items]
        if not rect or "c" not in kinds or "l" in kinds:
            continue
        if not (40 <= rect.width <= 180 and 40 <= rect.height <= 180):
            continue
        if abs(rect.width - rect.height) > 24:
            continue
        found.append(pymupdf.Rect(rect))
    merged = []
    for rect in found:
        placed = False
        for index, have in enumerate(merged):
            if have.intersects(rect):
                merged[index] = have | rect
                placed = True
                break
        if not placed:
            merged.append(rect)
    return merged


def signature_spots(page):
    """Each signature or initial line, with the ink and the printed name on it.

    Returned as fractions of the displayed page: (x0, y0, x1, y1).
    """
    pymupdf = _pymupdf()
    words = page.get_text("words") or []
    inks = _ink_rects(page)
    used = set()
    spots = []
    rows = _cluster_rules(_rule_lines(page))
    rows.sort(key=lambda row: row["y"])
    for row_index, row in enumerate(rows):
        attached = []
        for index, ink in enumerate(inks):
            overlaps_x = ink.x1 > row["x0"] - 8 and ink.x0 < row["x1"] + 8
            sits_on_rule = row["y"] - 40 <= ink.y1 <= row["y"] + 8
            if overlaps_x and sits_on_rule:
                attached.append(ink)
                used.add(index)
        under = _words_under(words, row["x0"], row["x1"], row["y"])
        beside = []
        if row["x1"] - row["x0"] <= 160:
            beside = _words_left_of_short_rule(words, row["x0"], row["y"])
        pieces = [(ink.x0, ink.y0, ink.x1, ink.y1) for ink in attached]
        pieces.extend(word[:4] for word in under)
        pieces.extend(word[:4] for word in beside)
        above = 4 if attached else 26
        pieces.append((row["x0"], row["y"] - above, row["x1"], row["y"] + 3))
        rect = _union(pieces, (row["x0"], row["y"] - above, row["x1"], row["y"] + 3))
        if row_index:
            previous = rows[row_index - 1]["y"]
            if row["y"] - previous < 55:
                rect.y0 = max(rect.y0, previous + 20)
        rect = pymupdf.Rect(rect.x0 - 6, rect.y0 - 3, rect.x1 + 6, rect.y1 + 4)
        window = _fraction_rect(page, rect)
        if window:
            spots.append(window)
    for index, ink in enumerate(inks):
        if index in used:
            continue
        under = _words_under(words, ink.x0, ink.x1, ink.y1)
        pieces = [(ink.x0, ink.y0, ink.x1, ink.y1)]
        pieces.extend(word[:4] for word in under)
        rect = _union(pieces, ink)
        rect = pymupdf.Rect(rect.x0 - 6, rect.y0 - 4, rect.x1 + 6, rect.y1 + 4)
        window = _fraction_rect(page, rect)
        if window:
            spots.append(window)
    for stamp in _stamp_rects(page):
        rect = pymupdf.Rect(stamp.x0 - 1, stamp.y0 - 2, stamp.x1 + 1, stamp.y1 + 2)
        window = _fraction_rect(page, rect)
        if window:
            spots.append(window)
    return spots


def _point_box(box, width, height):
    x, y, w, h = box
    return (x * width, y * height, (x + w) * width, (y + h) * height)


def _edge_gaps(box, spot):
    horizontal = max(0.0, spot[0] - box[2], box[0] - spot[2])
    vertical = max(0.0, spot[1] - box[3], box[1] - spot[3])
    return horizontal, vertical


def _snap_cost(box, spot):
    """How well a model box belongs to a real signature line. None keeps the model box.

    The model often boxes the sentence and misses the line beside it or just
    below it. Overlap always counts. A wide line does not claim the block under it.
    """
    horizontal, vertical = _edge_gaps(box, spot)
    center_y = (box[1] + box[3]) / 2.0
    above = box[3] <= spot[1] + 12
    left = box[2] <= spot[0] + 16
    spot_width = spot[2] - spot[0]
    shares_row = box[3] >= spot[1] - 8 and box[1] <= spot[3] + 8
    if horizontal == 0 and vertical == 0:
        return abs(center_y - (spot[1] + spot[3]) / 2.0) * 0.2
    near = spot[1] - 20 <= center_y <= spot[3] + 20
    if horizontal <= 24 and vertical <= 16 and near:
        return horizontal + vertical + 4
    if above and vertical <= 28 and horizontal <= 40:
        return vertical + horizontal + 8
    if left and spot_width <= 190 and horizontal <= 380 and (shares_row or vertical <= 28):
        return vertical + horizontal * 0.35 + 12
    return None


def _fallback_window(box, page_width, page_height):
    """Small pad around the model box when the page has no line to snap to.

    A tall or wide box is trimmed. It is not grown into a column of body text.
    """
    x, y, w, h = box
    pad_x = _inch(page_width, 0.14)
    pad_y = _inch(page_height, 0.08)
    x0 = max(0.0, x - pad_x)
    y0 = max(0.0, y - pad_y)
    x1 = min(1.0, x + w + pad_x)
    y1 = min(1.0, y + h + pad_y)
    max_w = min(0.62, _inch(page_width, 4.4))
    max_h = min(0.2, _inch(page_height, 1.4))
    if x1 - x0 > max_w:
        center = min(max(x + w / 2.0, max_w / 2.0), 1.0 - max_w / 2.0)
        x0 = center - max_w / 2.0
        x1 = x0 + max_w
    if y1 - y0 > max_h:
        y1 = min(1.0, y + h + pad_y)
        y0 = max(0.0, y1 - max_h)
    return (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))


def _word_windows(page):
    """Each word as fractions of the displayed page. Keeps short words."""
    pymupdf = _pymupdf()
    bounds = page.rect
    width = max(bounds.width, 1)
    height = max(bounds.height, 1)
    windows = []
    for word in page.get_text("words") or []:
        shown = _displayed_rect(page, pymupdf.Rect(word[:4]))
        windows.append((
            (shown.x0 - bounds.x0) / width,
            (shown.y0 - bounds.y0) / height,
            (shown.x1 - bounds.x0) / width,
            (shown.y1 - bounds.y0) / height,
        ))
    return windows


def context_window(page, window):
    """Pull the crop back a little so the mark can be placed on the page.

    A mark narrower than the text column is widened to that column. The crop
    then keeps the lines it touches, instead of cutting them off mid-word.
    """
    x0, y0, x1, y1 = window
    width = max(page.rect.width, 1)
    height = max(page.rect.height, 1)
    if (x1 - x0) < _inch(width, 5.5):
        margin = _inch(width, 0.35)
        x0 = min(x0, margin)
        x1 = max(x1, 1.0 - margin)
    else:
        pad_x = _inch(width, 0.28)
        x0 = max(0.0, x0 - pad_x)
        x1 = min(1.0, x1 + pad_x)
    words = _word_windows(page)

    def hits(word, left, top, right, bottom):
        return word[2] > left and word[0] < right and word[3] > top and word[1] < bottom

    def absorb(left, top, right, bottom):
        new_top, new_bottom = top, bottom
        for word in words:
            if not hits(word, left, top, right, bottom):
                continue
            new_top = min(new_top, word[1])
            new_bottom = max(new_bottom, word[3])
        return new_top, new_bottom

    def cover(left, top, right, bottom):
        for word in words:
            if word[3] <= top or word[1] >= bottom:
                continue
            left = min(left, word[0])
            right = max(right, word[2])
        return left, right

    pad_y = _inch(height, 0.62)
    band_top, band_bottom = absorb(x0, max(0.0, y0 - pad_y), x1, min(1.0, y1 + pad_y))
    breath = _inch(height, 0.1)
    band_top, band_bottom = absorb(
        x0, max(0.0, band_top - breath), x1, min(1.0, band_bottom + breath),
    )
    x0, x1 = cover(x0, band_top, x1, band_bottom)
    band_top, band_bottom = absorb(x0, band_top, x1, band_bottom)
    x0, x1 = cover(x0, band_top, x1, band_bottom)
    band_top, band_bottom = absorb(x0, band_top, x1, band_bottom)

    def room(edge_at, before):
        gap = None
        for word in words:
            if word[2] <= x0 or word[0] >= x1:
                continue
            if before and word[3] <= edge_at + 0.001:
                dist = edge_at - word[3]
            elif not before and word[1] >= edge_at - 0.001:
                dist = word[1] - edge_at
            else:
                continue
            gap = dist if gap is None else min(gap, dist)
        if gap is None:
            return _inch(height, 0.08)
        return min(_inch(height, 0.08), max(0.0, gap - _inch(height, 0.03)))

    return (
        round(max(0.0, x0 - _inch(width, 0.06)), 4),
        round(max(0.0, band_top - room(band_top, True)), 4),
        round(min(1.0, x1 + _inch(width, 0.06)), 4),
        round(min(1.0, band_bottom + room(band_bottom, False)), 4),
    )


def _choose_windows(page, boxes):
    """Each model box, and whether it landed on a real signature line.

    Only a matched line is pulled back for context. A box with no line nearby
    stays on what the model marked, so a paragraph is not widened into a column.
    """
    bounds = page.rect
    windows_on_page = signature_spots(page)
    spots = [
        _point_box((window[0], window[1], window[2] - window[0], window[3] - window[1]),
                   bounds.width, bounds.height)
        for window in windows_on_page
    ]
    chosen = [None] * len(boxes)
    pairs = []
    point_boxes = [_point_box(box, bounds.width, bounds.height) for box in boxes]
    for index, box in enumerate(point_boxes):
        for spot_index, spot in enumerate(spots):
            cost = _snap_cost(box, spot)
            if cost is not None:
                pairs.append((cost, index, spot_index))
    pairs.sort()
    used_spots = set()
    for cost, index, spot_index in pairs:
        if chosen[index] is not None:
            continue
        if spot_index in used_spots and cost > 36:
            continue
        chosen[index] = windows_on_page[spot_index]
        used_spots.add(spot_index)
    framed = []
    for box, spot in zip(boxes, chosen):
        if spot is None:
            framed.append((_fallback_window(box, bounds.width, bounds.height), False))
        else:
            framed.append((spot, True))
    return framed


def assign_windows(page, boxes):
    """Crop window for each model box, snapped onto a real line when one is close."""
    return [window for window, _matched in _choose_windows(page, boxes)]


def _jpeg_region(pix, x0, y0, x1, y1):
    """Cut a rectangle out of a rendered page. Fractions use the displayed page."""
    width, height = pix.width, pix.height
    if width < 1 or height < 1 or pix.n != 3:
        return None
    px0 = max(0, min(width - 1, int(x0 * width)))
    py0 = max(0, min(height - 1, int(y0 * height)))
    px1 = max(px0 + 1, min(width, int(round(x1 * width))))
    py1 = max(py0 + 1, min(height, int(round(y1 * height))))
    row_bytes = (px1 - px0) * pix.n
    samples = pix.samples
    rows = []
    for y in range(py0, py1):
        start = (y * width + px0) * pix.n
        rows.append(samples[start:start + row_bytes])
    pymupdf = _pymupdf()
    cropped = pymupdf.Pixmap(pymupdf.csRGB, px1 - px0, py1 - py0, b"".join(rows), False)
    return cropped.tobytes("jpeg", jpg_quality=82)


def render_page_jpeg(doc, page_number):
    page = doc[page_number - 1]
    return _render_page(page, 1600).tobytes("jpeg", jpg_quality=82)


def _finish_words(page, window):
    """Keep a model box from slicing a word it already crosses.

    Word boxes can overlap the next word, so this repeats until the crop
    no longer stops inside a word.
    """
    left, top, right, bottom = window
    words = _word_windows(page)
    for _ in range(12):
        grew = False
        for word in words:
            if word[2] <= left or word[0] >= right or word[3] <= top or word[1] >= bottom:
                continue
            if word[0] < left - 1e-6 or word[1] < top - 1e-6 or word[2] > right + 1e-6 or word[3] > bottom + 1e-6:
                left = min(left, word[0])
                top = min(top, word[1])
                right = max(right, word[2])
                bottom = max(bottom, word[3])
                grew = True
        if not grew:
            break
    return (
        round(max(0.0, left), 4), round(max(0.0, top), 4),
        round(min(1.0, right), 4), round(min(1.0, bottom), 4),
    )


def _shown_window(page, window, matched):
    if not matched:
        return _finish_words(page, window)
    return context_window(page, window)


def crop_jpeg(doc, page_number, box):
    page = doc[page_number - 1]
    window, matched = _choose_windows(page, [box])[0]
    return _jpeg_region(_render_page(page, 2000), *_shown_window(page, window, matched))


def _scan_doc(doc, name, file_index, firm_id, on_progress=None):
    def too_big(start, end):
        return len(slice_pdf(doc, start, end)) > CHUNK_BYTES

    found = []
    for start, end in chunk_spans(doc.page_count, too_big):
        if on_progress:
            if start == end:
                on_progress(f"Reading page {start + 1}")
            else:
                on_progress(f"Reading pages {start + 1}–{end + 1}")
        raw_items = _scan_chunk(slice_pdf(doc, start, end), start + 1, end + 1, firm_id)
        found.extend(normalize_chunk(raw_items, start + 1, end + 1, name, file_index))
    return mark_uncertain(dedupe_items(found))


def _verify_doc(doc, items, openai_client, firm_id, on_progress=None):
    pages = sorted({item["page"] for item in items if item["status"] == "ambiguous"})
    if not pages or openai_client is None:
        return items
    by_page = {}
    rest = []
    for item in items:
        if item["page"] in pages:
            by_page.setdefault(item["page"], []).append(item)
        else:
            rest.append(item)
    checked = []
    for page in pages:
        if on_progress:
            on_progress(f"Rechecking page {page}")
        try:
            image = render_page_jpeg(doc, page)
            checked.extend(_verify_page(openai_client, image, page, by_page[page], firm_id))
        except Exception:
            log.warning("Signature page render failed on page %s", page)
            checked.extend(by_page[page])
    return rest + checked


def _crop_items(doc, items):
    crops = {}
    by_page = {}
    for item in items:
        by_page.setdefault(item["page"], []).append(item)
    for page_number, group in by_page.items():
        try:
            page = doc[page_number - 1]
            pix = _render_page(page, 2000)
        except Exception:
            log.warning("Signature crop failed on page %s", page_number)
            continue
        try:
            choices = _choose_windows(page, [item["box"] for item in group])
        except Exception:
            log.warning("Signature crop failed on page %s", page_number)
            continue
        for item, (window, matched) in zip(group, choices):
            try:
                image = _jpeg_region(pix, *_shown_window(page, window, matched))
            except Exception:
                log.warning("Signature crop failed on page %s", page_number)
                image = None
            if not image:
                continue
            x0, y0, x1, y1 = window
            item["box"] = (x0, y0, round(x1 - x0, 4), round(y1 - y0, 4))
            item["region"] = region_label(item["box"])
            crops[item["id"]] = image
    return crops


def validate_documents(uploads, openai_client, firm_id=None, on_progress=None):
    """Check each PDF. Returns ({pages, files, items}, {item id: jpeg bytes})."""
    uploads = prepare_uploads(uploads)
    items = []
    crops = {}
    documents = []
    total_pages = 0
    total = len(uploads)
    for index, (name, data) in enumerate(uploads):
        def report(message, doc_index=index):
            if not on_progress:
                return
            if total > 1:
                on_progress(f"Document {doc_index + 1} of {total}: {message}")
            else:
                on_progress(message)

        report("Opening the document")
        doc = open_pdf(data, name)
        try:
            total_pages += doc.page_count
            documents.append({"index": index, "name": name, "pages": doc.page_count})
            file_items = _scan_doc(doc, name, index, firm_id, on_progress=report)
            file_items = _verify_doc(doc, file_items, openai_client, firm_id, on_progress=report)
            report("Preparing screenshots")
            for item in file_items:
                item["region"] = region_label(item["box"])
            crops.update(_crop_items(doc, file_items))
            items.extend(file_items)
        finally:
            doc.close()
    items = _sort_items(items)
    return {
        "pages": total_pages,
        "files": total,
        "documents": documents,
        "items": [public_item(item) for item in items],
    }, crops
