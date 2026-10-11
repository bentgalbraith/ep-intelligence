import unittest
from datetime import date

import validate_signatures as sig
from ai_logger import _compute_cost, _gemini_38_flash_pricing


def _dark_pixels(pix):
    samples = pix.samples
    count = 0
    for i in range(0, len(samples), pix.n):
        if samples[i] < 80 and samples[i + 1] < 80 and samples[i + 2] < 80:
            count += 1
    return count


def _red_pixels(pix):
    samples = pix.samples
    count = 0
    for i in range(0, len(samples), pix.n):
        if samples[i] > 180 and samples[i + 1] < 80 and samples[i + 2] < 80:
            count += 1
    return count


def _red_bounds(pix):
    samples = pix.samples
    min_x = min_y = 10 ** 9
    max_x = max_y = -1
    for y in range(pix.height):
        row = y * pix.width * pix.n
        for x in range(pix.width):
            i = row + x * pix.n
            if samples[i] > 180 and samples[i + 1] < 80 and samples[i + 2] < 80:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x + 1)
                max_y = max(max_y, y + 1)
    if max_x < 0:
        return None, None, None, None
    return min_x, min_y, max_x, max_y


def _jpeg_pixmap(data):
    import pymupdf
    image = pymupdf.open(stream=data, filetype="jpeg")
    try:
        return image[0].get_pixmap(colorspace=pymupdf.csRGB, alpha=False)
    finally:
        image.close()


def item(**kwargs):
    base = {
        "id": kwargs.get("id", "a" * 32),
        "file": "Trust.pdf",
        "file_index": 0,
        "page": 2,
        "type": "signature",
        "status": "ambiguous",
        "box": (0.1, 0.8, 0.4, 0.08),
        "label": "Grantor",
        "date_line": "unclear",
        "confidence": "medium",
    }
    base.update(kwargs)
    return base


class RulesTest(unittest.TestCase):
    def test_prepare_rejects_non_pdf(self):
        with self.assertRaises(sig.SignatureError):
            sig.prepare_uploads([("notes.docx", b"not a pdf")])

    def test_prepare_rejects_too_many(self):
        files = [(f"{i}.pdf", b"%PDF-1.4") for i in range(sig.MAX_FILES + 1)]
        with self.assertRaises(sig.SignatureError):
            sig.prepare_uploads(files)

    def test_duplicate_names(self):
        ready = sig.prepare_uploads([
            ("scan.pdf", b"%PDF-1.4"),
            ("scan.pdf", b"%PDF-1.7"),
        ])
        self.assertEqual([name for name, _ in ready], ["scan.pdf (1)", "scan.pdf (2)"])

    def test_chunk_spans_split_when_too_big(self):
        def too_big(start, end):
            return end - start > 2

        self.assertEqual(sig.chunk_spans(7, too_big, page_limit=15), [
            (0, 2), (3, 4), (5, 6),
        ])

    def test_chunk_spans_rejects_a_single_huge_page(self):
        with self.assertRaises(sig.SignatureError):
            sig.chunk_spans(2, lambda start, end: True, page_limit=15)

    def test_box_fractions_and_thousand_scale(self):
        self.assertEqual(sig.parse_box([0.1, 0.2, 0.3, 0.1])[:2], (0.1, 0.2))
        scaled = sig.parse_box([100, 800, 400, 80])
        self.assertEqual(scaled, (0.1, 0.8, 0.4, 0.08))

    def test_corner_box_is_used_when_width_and_height_run_off_the_page(self):
        # ymin, xmin, ymax, xmax on a 0-1000 grid, bottom-right of the page.
        box = sig.parse_box([800, 700, 920, 960])
        self.assertEqual(box, (0.7, 0.8, 0.26, 0.12))

    def test_long_pdf_name_keeps_its_extension(self):
        ready = sig.prepare_uploads([("a" * 200 + ".pdf", b"%PDF-1.4")])
        self.assertTrue(ready[0][0].lower().endswith(".pdf"))
        self.assertLessEqual(len(ready[0][0]), 180)

    def test_crop_window_includes_ink_above_a_line(self):
        # A thin line near the bottom of a letter page.
        x0, y0, x1, y1 = sig.context_fractions((0.12, 0.88, 0.08, 0.015), 612, 792)
        self.assertLess(y0, 0.80)
        self.assertGreater(y1, 0.89)
        self.assertGreater(x1 - x0, 0.35)
        self.assertGreaterEqual(x0, 0)
        self.assertLessEqual(y1, 1)

    def test_thin_box_is_expanded(self):
        box = sig.parse_box([0.5, 0.9, 0.01, 0.005])
        self.assertGreaterEqual(box[2], 0.08)
        self.assertGreaterEqual(box[3], 0.02)
        self.assertLessEqual(box[0] + box[2], 1)
        self.assertLessEqual(box[1] + box[3], 1)

    def test_region_labels(self):
        self.assertEqual(sig.region_label((0.7, 0.8, 0.2, 0.1)), "bottom right")
        self.assertEqual(sig.region_label((0.4, 0.05, 0.2, 0.1)), "top")
        self.assertEqual(sig.region_label((0.05, 0.4, 0.2, 0.1)), "middle left")

    def test_resolve_page_counts_from_the_start_of_this_file(self):
        self.assertEqual(sig.resolve_page(1, 16, 30), 16)
        self.assertEqual(sig.resolve_page(15, 16, 30), 30)
        self.assertIsNone(sig.resolve_page(16, 16, 30))
        self.assertIsNone(sig.resolve_page(99, 16, 30))

    def test_dedupe_keeps_the_surer_overlap(self):
        low = item(id="1", confidence="low", box=(0.1, 0.1, 0.4, 0.2))
        high = item(id="2", confidence="high", box=(0.12, 0.12, 0.4, 0.2))
        other = item(id="3", type="notary", confidence="low", box=(0.12, 0.12, 0.4, 0.2))
        kept = sig.dedupe_items([low, high, other])
        ids = {row["id"] for row in kept}
        self.assertEqual(ids, {"2", "3"})

    def test_low_confidence_becomes_unclear(self):
        rows = sig.mark_uncertain([item(status="completed", confidence="low")])
        self.assertEqual(rows[0]["status"], "ambiguous")
        self.assertNotIn("confidence", rows[0])
        self.assertEqual(rows[0]["region"], "bottom left")

    def test_verification_upgrades_only_unclear_items(self):
        rows = [
            item(id="keep", status="ambiguous"),
            item(id="sure", status="completed", label=""),
        ]
        merged = sig.apply_verification(rows, [
            {"id": "keep", "status": "blank", "date_line": "blank"},
            {"id": "sure", "status": "blank", "label": "Trustee"},
            {"id": "missing", "status": "completed", "type": "initial", "box": [0.1, 0.1, 0.2, 0.05]},
            {
                "type": "notary",
                "status": "completed",
                "box": [0.2, 0.7, 0.5, 0.15],
                "label": "Notary acknowledgment",
                "date_line": "filled",
            },
        ])
        by_id = {row["id"]: row for row in merged}
        self.assertEqual(by_id["keep"]["status"], "blank")
        self.assertEqual(by_id["keep"]["date_line"], "blank")
        self.assertEqual(by_id["sure"]["status"], "ambiguous")
        self.assertEqual(by_id["sure"]["label"], "Trustee")
        self.assertEqual(len(merged), 3)
        added = merged[-1]
        self.assertEqual(added["type"], "notary")
        self.assertEqual(added["page"], 2)
        self.assertNotIn("box", sig.public_item(added))

    def test_gemini_text_skips_thoughts(self):
        text, finish = sig._gemini_text({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [
                    {"thought": True, "text": "hidden reasoning"},
                    {"text": '{"items": []}'},
                ]},
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4, "thoughtsTokenCount": 6},
        })
        self.assertEqual(text, '{"items": []}')
        self.assertEqual(finish, "STOP")
        self.assertNotIn("hidden", text)
        self.assertEqual(sig._gemini_usage({
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4, "thoughtsTokenCount": 6},
        }), (10, 4, 6))

    def test_normalize_drops_a_page_outside_the_chunk(self):
        found = sig.normalize_chunk(
            [{"type": "signature", "status": "blank", "page": 4, "box": [0.1, 0.2, 0.3, 0.1],
              "label": "Witness", "date_line": "none", "confidence": "high"}],
            1, 2, "Will.pdf", 0,
        )
        self.assertEqual(found, [])

    def test_too_many_items_is_an_error(self):
        raw = [{
            "type": "signature", "status": "blank", "page": 1,
            "box": [0.1, 0.2, 0.3, 0.1], "label": "Witness",
            "date_line": "none", "confidence": "high",
        }] * (sig.MAX_ITEMS_PER_CHUNK + 1)
        with self.assertRaises(sig.SignatureError):
            sig.normalize_chunk(raw, 1, 1, "Will.pdf", 0)


class PricingTest(unittest.TestCase):
    def test_existing_openai_price_is_unchanged(self):
        self.assertEqual(_compute_cost("openai", "gpt-5.6-terra", 1000, 100, 0, None), 0.004)

    def test_gemini_introductory_and_standard_rates(self):
        intro = _gemini_38_flash_pricing(date(2026, 10, 11))
        later = _gemini_38_flash_pricing(date(2027, 1, 1))
        self.assertEqual(intro["input"], 0.75e-6)
        self.assertEqual(later["input"], 1.50e-6)
        self.assertEqual(later["output"], 7.50e-6)
        cost = _compute_cost("gemini", "gemini-3.8-flash", 56000, 2000, 4000, 100)
        self.assertIsNotNone(cost)
        self.assertGreater(cost, 0)

    def test_gpt6_sol_price(self):
        self.assertEqual(_compute_cost("openai", "gpt-6-sol", 2000, 100, 50, None), 0.0055)

    def test_document_ai_price_is_unchanged(self):
        self.assertEqual(_compute_cost("google_documentai", None, None, None, None, 10), 0.015)


class PdfTest(unittest.TestCase):
    def test_crop_includes_writing_above_the_line(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        # Red ink above a signature line near the bottom of the page.
        page.draw_rect(pymupdf.Rect(80, 640, 200, 695), color=(1, 0, 0), fill=(1, 0, 0), width=0)
        page.draw_line(pymupdf.Point(72, 704), pymupdf.Point(280, 704), width=1.2)
        line = (72 / 612, 700 / 792, 208 / 612, 8 / 792)
        image = sig.crop_jpeg(doc, 1, line)
        doc.close()
        self.assertGreater(_red_pixels(_jpeg_pixmap(image)), 0)

    def test_crop_is_a_jpeg(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(72, 700, 280, 740), color=(0, 0, 0), width=1)
        image = sig.crop_jpeg(doc, 1, (0.1, 0.85, 0.35, 0.06))
        doc.close()
        self.assertTrue(image.startswith(b"\xff\xd8"))

    def test_rotated_page_crop_follows_the_displayed_page(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.set_rotation(90)
        page.draw_rect(pymupdf.Rect(0, 0, 80, 80), color=(1, 0, 0), fill=(1, 0, 0), width=0)
        full = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), colorspace=pymupdf.csRGB, alpha=False)
        x0, y0, x1, y1 = _red_bounds(full)
        self.assertIsNotNone(x0)
        box = (x0 / full.width, y0 / full.height, (x1 - x0) / full.width, (y1 - y0) / full.height)
        hit = _jpeg_pixmap(sig.crop_jpeg(doc, 1, box))
        miss = _jpeg_pixmap(sig.crop_jpeg(doc, 1, (0.0, 0.75, 0.2, 0.2)))
        doc.close()
        self.assertGreater(_red_pixels(hit), 0)
        self.assertEqual(_red_pixels(miss), 0)

    def test_open_rejects_garbage(self):
        with self.assertRaises(sig.SignatureError) as caught:
            sig.open_pdf(b"%PDF-not-really", name="Smith Will.pdf")
        self.assertIn("Smith Will.pdf", str(caught.exception))
        self.assertNotIn("Smith", caught.exception.notes)
        self.assertNotIn("Smith", caught.exception.alert)


if __name__ == "__main__":
    unittest.main()
