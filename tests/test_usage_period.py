"""Custom date range on the usage dashboards. No database connection."""

import os
import time
import unittest
from datetime import date, datetime, timedelta

os.environ["SECRET_KEY"] = "test"
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["OPENAI_API_KEY"] = "test"

import app as appmod
import tracker_db


class PeriodParseTest(unittest.TestCase):
    def test_presets(self):
        self.assertEqual(appmod._parse_usage_period("7", None, None)[:2], (7, "7"))
        self.assertEqual(appmod._parse_usage_period("all", None, None)[:2], (None, "all"))
        self.assertEqual(appmod._parse_usage_period("nope", None, None)[:2], (30, "30"))
        self.assertIsNone(appmod._parse_usage_period("30", None, None)[2])

    def test_custom_range(self):
        days, key, start, end = appmod._parse_usage_period("custom", "2026-01-01", "2026-01-31")
        self.assertEqual((days, key, start, end), (None, "custom", date(2026, 1, 1), date(2026, 1, 31)))

    def test_invalid_custom_falls_back(self):
        self.assertEqual(appmod._parse_usage_period("custom", "2026-02-01", "2026-01-01")[:2], (30, "30"))
        self.assertEqual(appmod._parse_usage_period("custom", "", "")[:2], (30, "30"))
        self.assertEqual(appmod._parse_usage_period("custom", "2999-01-01", "2999-02-01")[:2], (30, "30"))

    def test_a_two_year_range_is_kept(self):
        _days, key, start, end = appmod._parse_usage_period("custom", "2024-01-01", "2026-01-01")
        self.assertEqual((key, start, end), ("custom", date(2024, 1, 1), date(2026, 1, 1)))

    def test_a_multi_decade_range_keeps_the_latest_20_years(self):
        _days, key, start, end = appmod._parse_usage_period("custom", "1900-01-01", "2026-01-01")
        self.assertEqual(key, "custom")
        self.assertEqual(end, date(2026, 1, 1))
        self.assertEqual((end - start).days, appmod._MAX_CUSTOM_DAYS)

    def test_prior_period_is_the_preceding_calendar_days(self):
        prior = tracker_db._custom_prior_start(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(prior.date(), date(2026, 1, 29))
        self.assertEqual((prior.hour, prior.minute), (0, 0))
        one_day = tracker_db._custom_prior_start(date(2026, 3, 1), date(2026, 3, 1))
        self.assertEqual(one_day.date(), date(2026, 2, 28))

    def test_hourly_buckets_match_a_chosen_day(self):
        hour = appmod._as_et_hour(datetime(2026, 1, 15, 14, 0))
        first, last = appmod._calendar_day_hours(date(2026, 1, 15))
        cursor = first
        found = False
        while cursor <= last:
            if cursor == hour:
                found = True
                break
            cursor += timedelta(hours=1)
        self.assertTrue(found)

    def test_end_is_clamped_to_today(self):
        today = appmod.datetime.now(appmod._EASTERN).date()
        _days, key, start, end = appmod._parse_usage_period("custom", "2020-01-01", "2999-01-01")
        self.assertEqual(key, "custom")
        self.assertEqual(start, date(2020, 1, 1))
        self.assertEqual(end, today)

    def test_bounds_are_eastern_midnights(self):
        start_ts, end_ts = tracker_db._range_bounds(date(2026, 1, 1), date(2026, 1, 1))
        self.assertEqual(end_ts - start_ts, timedelta(days=1))
        where, params = tracker_db._usage_filters(start=date(2026, 1, 1), end=date(2026, 1, 2))
        self.assertIn("l.timestamp >= %s AND l.timestamp < %s", where)
        self.assertEqual(params, [start_ts, tracker_db._range_bounds(date(2026, 1, 1), date(2026, 1, 2))[1]])

    def test_preset_filter_stays_a_rolling_window(self):
        where, params = tracker_db._usage_filters(days=30)
        self.assertIn("INTERVAL '1 day'", where)
        self.assertEqual(params, [30])


class ChartAxisTest(unittest.TestCase):
    def setUp(self):
        self.saved_url = appmod.tracker_db.DATABASE_URL
        self.saved_day = appmod.tracker_db.usage_cost_by_day
        self.saved_hour = appmod.tracker_db.usage_cost_by_hour
        self.saved_firm_day = appmod.tracker_db.firm_usage_by_day_tool
        appmod.tracker_db.DATABASE_URL = "postgres://unused"

    def tearDown(self):
        appmod.tracker_db.DATABASE_URL = self.saved_url
        appmod.tracker_db.usage_cost_by_day = self.saved_day
        appmod.tracker_db.usage_cost_by_hour = self.saved_hour
        appmod.tracker_db.firm_usage_by_day_tool = self.saved_firm_day

    def test_custom_days_stop_at_the_chosen_end(self):
        def fake(**kwargs):
            self.kwargs = kwargs
            return [{"day": date(2026, 3, 2), "cost": 2}]

        appmod.tracker_db.usage_cost_by_day = fake
        series = appmod._usage_cost_series(None, None, None, start=date(2026, 3, 1), end=date(2026, 3, 3))
        self.assertEqual([point["date"] for point in series], ["2026-03-01", "2026-03-02", "2026-03-03"])
        self.assertEqual(series[-1]["cost"], 2)
        self.assertEqual(self.kwargs["start"], date(2026, 3, 1))
        self.assertIsNone(self.kwargs["days"])

    def test_long_custom_range_is_weekly(self):
        appmod.tracker_db.usage_cost_by_day = lambda **kwargs: []
        series = appmod._usage_cost_series(None, None, None, start=date(2025, 1, 1), end=date(2026, 1, 1))
        self.assertLess(len(series), 60)
        self.assertGreater(len(series), 50)
        self.assertTrue(series[0]["tip"].startswith("Week of "))

    def test_single_custom_day_is_that_calendar_day(self):
        appmod.tracker_db.usage_cost_by_hour = lambda **kwargs: []
        series = appmod._usage_cost_series(None, None, None, start=date(2026, 1, 15), end=date(2026, 1, 15))
        self.assertEqual(len(series), 24)
        self.assertTrue(series[0]["date"].startswith("2026-01-15"))
        self.assertEqual(series[0]["label"], "12 AM")
        self.assertEqual(series[-1]["label"], "11 PM")

    def test_preset_still_runs_through_today(self):
        appmod.tracker_db.usage_cost_by_day = lambda **kwargs: []
        series = appmod._usage_cost_series(30, None, None)
        today = appmod.datetime.now(appmod._EASTERN).date()
        self.assertEqual(series[-1]["date"], today.isoformat())
        self.assertEqual(len(series), 31)

    def test_firm_chart_uses_the_chosen_days(self):
        def fake(*_args, **_kwargs):
            return [{"day": date(2026, 3, 2), "tool": "ep_extract", "calls": 4}]

        appmod.tracker_db.firm_usage_by_day_tool = fake
        chart = appmod._firm_usage_chart_series("firm", None, date(2026, 3, 1), date(2026, 3, 3))
        self.assertEqual(len(chart["points"]), 3)
        self.assertEqual(chart["points"][0]["total"], 0)
        self.assertEqual(chart["points"][1]["total"], 4)
        self.assertEqual(chart["tools"][0]["label"], "Drafting Notes")


class DashboardRenderTest(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as sess:
            sess["is_admin"] = True
            sess["admin_since"] = time.time()

    def test_admin_usage_and_feed_show_custom_dates(self):
        for path in ("/admin/usage", "/admin/feed"):
            resp = self.client.get(path + "?days=custom&from=2026-01-01&to=2026-01-10")
            self.assertEqual(resp.status_code, 200, path)
            html = resp.get_data(as_text=True)
            self.assertIn("Custom range", html)
            self.assertIn('value="custom" selected', html)
            self.assertIn('value="2026-01-01"', html)
            self.assertIn('value="2026-01-10"', html)
            self.assertIn('name="from"', html)
            self.assertIn("Update Custom Range", html)
            self.assertIn('data-applied-from="2026-01-01"', html)
            self.assertIn('data-applied-to="2026-01-10" disabled', html)
            self.assertIn("Filtered to Jan 1, 2026 – Jan 10, 2026", html)
            self.assertNotIn("usageCustomChanged", html)
            dates = html.split('class="usage-custom-dates"', 1)[1].split("</span>", 1)[0]
            self.assertNotIn("disabled", dates)

    def test_preset_hides_the_date_inputs(self):
        resp = self.client.get("/admin/usage")
        html = resp.get_data(as_text=True)
        self.assertIn('value="30" selected', html)
        dates = html.split('class="usage-custom-dates"', 1)[1].split("</span>", 1)[0]
        self.assertIn("hidden", html.split('class="usage-custom-dates"', 1)[0][-20:] + dates[:80])
        self.assertIn("disabled", dates)

    def test_firm_dashboard_template(self):
        with appmod.app.test_request_context("/firm-usage"):
            html = appmod.render_template(
                "firm_usage.html",
                firm_name="Example Firm",
                overview={"calls": 0, "last_seen": None, "people": 0, "tools": 0},
                people=[],
                tools=[],
                chart=None,
                show_costs=False,
                cost_series=[],
                **appmod._period_template("custom", date(2026, 4, 1), date(2026, 4, 2)),
            )
        self.assertIn("Custom range", html)
        self.assertIn('value="2026-04-01"', html)
        self.assertIn('value="2026-04-02"', html)
        self.assertIn("Filtered to Apr 1, 2026 – Apr 2, 2026", html)
        self.assertIn("Update Custom Range", html)


if __name__ == "__main__":
    unittest.main()
