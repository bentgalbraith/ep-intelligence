"""Shared quote verification: fuzzy-match AI-generated quotes against source text."""

import difflib
import logging
import re

log = logging.getLogger("quote_verify")


def normalize(text):
    """Collapse whitespace and lowercase for comparison."""
    return re.sub(r"\s+", " ", text.strip().lower())


def build_norm_to_orig_map(source_text):
    """Build a mapping from normalized string positions back to original source positions."""
    norm_chars = []
    orig_indices = []
    i = 0
    text = source_text.lower()
    while i < len(text):
        if text[i].isspace():
            norm_chars.append(" ")
            orig_indices.append(i)
            while i < len(text) and text[i].isspace():
                i += 1
        else:
            norm_chars.append(text[i])
            orig_indices.append(i)
            i += 1
    norm_str = "".join(norm_chars)
    # Strip leading/trailing spaces and trim orig_indices to match
    lstrip_count = len(norm_str) - len(norm_str.lstrip())
    rstrip_count = len(norm_str) - len(norm_str.rstrip())
    if rstrip_count:
        orig_indices = orig_indices[lstrip_count:-rstrip_count]
    else:
        orig_indices = orig_indices[lstrip_count:]
    return norm_str.strip(), orig_indices


def find_original_text(segment, source, norm_source, orig_indices):
    """Find the actual source text for a segment. Returns (original_text, found)."""
    norm_seg = normalize(segment)
    if not norm_seg:
        return segment, True

    idx = norm_source.find(norm_seg)
    if idx != -1:
        orig_start = orig_indices[idx]
        orig_end = orig_indices[min(idx + len(norm_seg) - 1, len(orig_indices) - 1)] + 1
        real_text = source[orig_start:orig_end]
        log.info("    [EXACT] '%s'", norm_seg[:80])
        return real_text, True

    seg_len = len(norm_seg)
    if seg_len > len(norm_source):
        log.info("    [OVERSIZED] segment longer than source — dropping")
        return None, False

    best_ratio = 0.0
    best_idx = 0
    step = max(1, seg_len // 8)
    for i in range(0, len(norm_source) - seg_len + 1, step):
        window = norm_source[i:i + seg_len]
        ratio = difflib.SequenceMatcher(None, norm_seg, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i
            if best_ratio >= 0.98:
                break

    if best_ratio >= 0.9:
        orig_start = orig_indices[best_idx]
        orig_end = orig_indices[min(best_idx + seg_len - 1, len(orig_indices) - 1)] + 1
        while orig_start > 0 and not source[orig_start - 1].isspace():
            orig_start -= 1
        while orig_end < len(source) and not source[orig_end].isspace():
            orig_end += 1
        real_text = source[orig_start:orig_end].strip()
        log.info("    [FUZZY %.3f] '%s' → '%s'", best_ratio, norm_seg[:60], real_text[:60])
        return real_text, True

    log.info("    [DROPPED %.3f] '%s'", best_ratio, norm_seg[:80])
    return None, False


def verify_single_quote(quote, source, norm_source, orig_indices):
    """Verify a single quote. Keeps verified segments, skips bad ones."""
    segments = [s.strip() for s in quote.split(" ... ") if s.strip()]
    if not segments:
        return None

    log.info("  Quote has %d segment(s)", len(segments))
    verified_segments = []
    for idx, seg in enumerate(segments):
        log.info("  --- Segment %d: '%s'", idx, seg[:100])
        real_text, found = find_original_text(seg, source, norm_source, orig_indices)
        if not found:
            log.info("  [SEGMENT SKIPPED] segment %d not verified", idx)
            continue
        verified_segments.append(real_text)

    if not verified_segments:
        log.info("  [QUOTE DROPPED] no segments verified")
        return None

    result = " ... ".join(verified_segments)
    log.info("  [QUOTE KEPT] %s", result[:150])
    return result


def verify_quotes(extraction, source_text):
    """Verify all quotes in an extraction dict against the source text.

    Mutates extraction in place: replaces quotes with verified text from source,
    or sets to None if verification fails.
    """
    norm_source, orig_indices = build_norm_to_orig_map(source_text)
    log.info("=== QUOTE VERIFICATION START (source len: %d) ===", len(norm_source))
    sections = extraction.get("sections", {})
    for sec in sections.values():
        if "fields" in sec:
            for field in sec["fields"].values():
                if not field or not field.get("quote"):
                    continue
                field["quote"] = verify_single_quote(
                    field["quote"], source_text, norm_source, orig_indices
                )
        if "entries" in sec:
            for entry in sec["entries"]:
                for field in entry.get("fields", {}).values():
                    if not field or not field.get("quote"):
                        continue
                    field["quote"] = verify_single_quote(
                        field["quote"], source_text, norm_source, orig_indices
                    )
