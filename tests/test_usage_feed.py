"""Admin feed: one row per time a person used a tool, not per model call."""

import unittest
from datetime import datetime, timedelta, timezone

import usage_tools


T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def at(minutes, tool, status="success", firm="firm-a", employee="E1", name="Ada",
       execution_ms=None):
    return {
        "timestamp": T0 + timedelta(minutes=minutes),
        "firm_id": firm,
        "firm_name": "Alpha" if firm == "firm-a" else "Beta",
        "employee_id_code": employee,
        "employee_name": name,
        "tool": tool,
        "status": status,
        "execution_ms": execution_ms,
    }


class FeedItemTest(unittest.TestCase):
    def test_a_page_by_page_run_is_one_use(self):
        rows = [at(i, "validate_signatures_verify", status="error") for i in range(150)]
        rows.append(at(150, "validate_signatures", execution_ms=150 * 60 * 1000))
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tool"], "Name Files & Validate Signatures")
        self.assertEqual(items[0]["status"], "success")
        self.assertEqual(items[0]["timestamp"], T0 + timedelta(minutes=150))

    def test_failed_hops_during_a_successful_run_stay_in_that_run(self):
        rows = [
            at(0, "validate_signatures_crop", status="error"),
            at(1, "validate_signatures_crop", status="error"),
            at(2, "validate_signatures"),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "success")

    def test_a_run_that_never_finished_is_still_one_use(self):
        rows = [at(i, "validate_signatures_scan", status="error") for i in range(4)]
        rows += [at(i, "validate_signatures_verify", status="error") for i in range(4, 8)]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tool"], "Name Files & Validate Signatures")
        self.assertEqual(items[0]["status"], "error")

    def test_two_runs_close_together_stay_separate(self):
        rows = [
            at(0, "validate_signatures_verify", status="error"),
            at(5, "validate_signatures"),
            at(6, "validate_signatures_verify", status="error"),
            at(10, "validate_signatures"),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["timestamp"], T0 + timedelta(minutes=10))
        self.assertEqual(items[1]["timestamp"], T0 + timedelta(minutes=5))

    def test_an_early_failure_joins_the_long_run_it_belongs_to(self):
        rows = [
            at(0, "validate_signatures_scan", status="error"),
            at(45, "validate_signatures", execution_ms=45 * 60 * 1000),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "success")
        self.assertEqual(items[0]["duration"], "45 min")

    def test_a_recent_failure_does_not_join_the_next_short_run(self):
        rows = [
            at(0, "validate_signatures_scan", status="error"),
            at(10, "validate_signatures", execution_ms=60 * 1000),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["status"], "success")
        self.assertEqual(items[1]["status"], "error")
        self.assertEqual(items[1]["tool"], "Name Files & Validate Signatures (scan)")

    def test_an_early_failure_does_not_join_a_later_short_run(self):
        rows = [
            at(0, "validate_signatures_scan", status="error"),
            at(45, "validate_signatures", execution_ms=2 * 60 * 1000),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["status"], "success")
        self.assertEqual(items[1]["status"], "error")

    def test_a_gap_splits_unfinished_runs(self):
        rows = [
            at(0, "doc_separator_ocr", status="error"),
            at(20, "doc_separator_ocr", status="error"),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["tool"], "Document Separator (OCR)")

    def test_a_redo_is_its_own_use(self):
        rows = [
            at(0, "doc_separator"),
            at(3, "client_doc_summary_part", status="error"),
            at(4, "doc_separator_redo"),
        ]
        # The part row is a different product, so it does not join the redo.
        items = usage_tools.feed_items(rows)
        tools = [item["tool"] for item in items]
        self.assertEqual(tools, [
            "Document Separator (Redo)",
            "Summarize Documents for Clients (part)",
            "Document Separator",
        ])

    def test_redo_hops_belong_to_the_redo(self):
        rows = [
            at(0, "client_doc_summary"),
            at(2, "client_doc_summary_part", status="error"),
            at(3, "client_doc_summary_part", status="error"),
            at(4, "client_doc_summary_redo"),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual([item["tool"] for item in items], [
            "Summarize Documents for Clients (redo)",
            "Summarize Documents for Clients",
        ])
        self.assertEqual(items[0]["status"], "success")

    def test_a_qprt_click_is_its_own_use(self):
        rows = [
            at(0, "doc_separator_ocr", status="error"),
            at(2, "doc_separator"),
            at(6, "doc_separator_qprt"),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual([item["tool"] for item in items], [
            "Document Separator (QPRT term)",
            "Document Separator",
        ])
        self.assertEqual(items[1]["status"], "success")

    def test_firms_and_people_are_not_combined(self):
        rows = [
            at(0, "ep_extract", firm="firm-a", employee="E1"),
            at(1, "ep_extract", firm="firm-b", employee="E1"),
            at(2, "ep_extract", firm="firm-a", employee="E2", name="Bea"),
        ]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 3)

    def test_two_quick_uses_of_a_tool_with_no_hops_stay_separate(self):
        rows = [at(0, "estate_tax_calc"), at(1, "estate_tax_calc")]
        items = usage_tools.feed_items(rows)
        self.assertEqual(len(items), 2)

    def test_newest_use_is_first(self):
        rows = [at(0, "tracker"), at(30, "ep_extract", status="error")]
        items = usage_tools.feed_items(rows)
        self.assertEqual(items[0]["tool"], "Drafting Notes")
        self.assertEqual(items[0]["status"], "error")
        self.assertEqual(items[1]["tool"], "Client Progress Tracker")

    def test_duration_is_the_recorded_runtime_only(self):
        spanned = [at(0, "validate_signatures_verify", status="error"), at(12, "validate_signatures")]
        self.assertEqual(usage_tools.feed_items(spanned)[0]["duration"], "")

        recorded = [
            at(0, "validate_signatures_verify", status="error"),
            at(1, "validate_signatures", execution_ms=20 * 60 * 1000),
        ]
        self.assertEqual(usage_tools.feed_items(recorded)[0]["duration"], "20 min")

        hops = [at(i, "validate_signatures_scan", status="error") for i in range(4)]
        self.assertEqual(usage_tools.feed_items(hops)[0]["duration"], "")

        self.assertEqual(usage_tools.format_feed_duration(None), "")
        self.assertEqual(usage_tools.format_feed_duration(0), "0 sec")
        self.assertEqual(usage_tools.format_feed_duration(90), "1 min")
        self.assertEqual(usage_tools.format_feed_duration(90 * 60), "1 hr 30 min")

    def test_hidden_sql_keeps_failed_hops_and_qprt(self):
        sql = usage_tools.feed_row_sql()
        self.assertIn("'validate_signatures_verify'", sql)
        self.assertNotIn("'doc_separator_qprt'", sql)
        self.assertIn("status = 'error'", sql)
        self.assertIn("'doc_separator_qprt'", usage_tools.firm_use_row_sql())
        self.assertIn("'validate_signatures_verify'", usage_tools.firm_use_row_sql())


if __name__ == "__main__":
    unittest.main()
