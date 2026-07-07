"""Document difference detection: compare a .docx against specimen documents."""

import difflib
import io
import re

from docx import Document
from docx.shared import Pt, RGBColor


def extract_text_blocks(docx_bytes):
    """Extract paragraph texts from a .docx file as a list of strings."""
    doc = Document(io.BytesIO(docx_bytes))
    blocks = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            blocks.append(text)
    return blocks


def compute_similarity(blocks_a, blocks_b):
    """Compute similarity ratio between two documents' text blocks (0.0–1.0)."""
    text_a = "\n".join(blocks_a)
    text_b = "\n".join(blocks_b)
    return difflib.SequenceMatcher(None, text_a, text_b).ratio()


def rank_specimens(upload_bytes, specimens):
    """Rank specimen documents by similarity to the uploaded document.

    Args:
        upload_bytes: raw .docx bytes of the uploaded document
        specimens: list of dicts with 'id', 'name', 'docx_data' (bytes)

    Returns:
        list of dicts: [{'id', 'name', 'similarity'}, ...] sorted desc by similarity
    """
    upload_blocks = extract_text_blocks(upload_bytes)
    results = []
    for spec in specimens:
        spec_blocks = extract_text_blocks(spec["docx_data"])
        sim = compute_similarity(upload_blocks, spec_blocks)
        results.append({
            "id": str(spec["id"]),
            "name": spec["name"],
            "similarity": round(sim * 100, 1),
        })
    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results


def build_diff_docx(upload_bytes, specimen_bytes):
    """Build a .docx with comments highlighting differences from the specimen.

    Returns a BytesIO containing the annotated .docx.
    """
    upload_doc = Document(io.BytesIO(upload_bytes))
    specimen_blocks = extract_text_blocks(specimen_bytes)

    upload_blocks = []
    for para in upload_doc.paragraphs:
        text = para.text.strip()
        if text:
            upload_blocks.append(text)

    matcher = difflib.SequenceMatcher(None, specimen_blocks, upload_blocks)
    opcodes = matcher.get_opcodes()

    diff_map = {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        elif tag == "replace":
            for idx in range(j1, j2):
                specimen_text = specimen_blocks[i1:i2]
                diff_map[idx] = f"Specimen had: \"{' | '.join(specimen_text)}\""
        elif tag == "insert":
            for idx in range(j1, j2):
                diff_map[idx] = "Added — not in specimen"
        elif tag == "delete":
            if j1 in diff_map:
                diff_map[j1] += f" | Removed from specimen: \"{' | '.join(specimen_blocks[i1:i2])}\""
            else:
                nearest = min(j1, len(upload_blocks) - 1)
                diff_map[nearest] = f"Removed from specimen: \"{' | '.join(specimen_blocks[i1:i2])}\""

    out_doc = Document()
    style = out_doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    upload_block_idx = 0
    for para in upload_doc.paragraphs:
        text = para.text.strip()
        if not text:
            out_doc.add_paragraph("")
            continue

        new_para = out_doc.add_paragraph()
        _copy_para_format(para, new_para)
        runs = []
        for run in para.runs:
            new_run = new_para.add_run(run.text)
            _copy_run_format(run, new_run)
            runs.append(new_run)

        if upload_block_idx in diff_map and runs:
            comment_text = diff_map[upload_block_idx]
            if len(comment_text) > 500:
                comment_text = comment_text[:500] + "..."
            out_doc.add_comment(runs=runs, text=comment_text, author="Diff")

        upload_block_idx += 1

    buf = io.BytesIO()
    out_doc.save(buf)
    buf.seek(0)
    return buf


def _copy_para_format(src, dst):
    """Copy basic paragraph formatting."""
    if src.paragraph_format.alignment is not None:
        dst.paragraph_format.alignment = src.paragraph_format.alignment


def _copy_run_format(src, dst):
    """Copy basic run formatting."""
    dst.bold = src.bold
    dst.italic = src.italic
    dst.underline = src.underline
    if src.font.size:
        dst.font.size = src.font.size
    if src.font.name:
        dst.font.name = src.font.name
