"""Employee allow-lists on firm tools. No database connection."""

import os
import unittest
import uuid
from unittest.mock import patch

os.environ["SECRET_KEY"] = "test"
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["OPENAI_API_KEY"] = "test"

from werkzeug.datastructures import ImmutableMultiDict

import app as appmod


class EmployeeMayUseToolTest(unittest.TestCase):
    def test_firm_off_denies_even_a_listed_employee(self):
        self.assertFalse(appmod.employee_may_use_tool(False, True, True, "a", ["a"]))

    def test_unlimited_allows_everyone(self):
        self.assertTrue(appmod.employee_may_use_tool(True, False, False, None, []))
        self.assertTrue(appmod.employee_may_use_tool(True, False, True, None, []))

    def test_limited_without_employee_login_denies(self):
        self.assertFalse(appmod.employee_may_use_tool(True, True, False, "a", ["a"]))

    def test_limited_allows_only_listed_employees(self):
        self.assertTrue(appmod.employee_may_use_tool(True, True, True, "a", ["a", "b"]))
        self.assertFalse(appmod.employee_may_use_tool(True, True, True, "c", ["a"]))
        self.assertFalse(appmod.employee_may_use_tool(True, True, True, None, ["a"]))
        self.assertFalse(appmod.employee_may_use_tool(True, True, True, "", ["a"]))

    def test_bad_allow_list_denies(self):
        self.assertFalse(appmod.employee_may_use_tool(True, True, True, "a", "a"))


class ToolAccessParseTest(unittest.TestCase):
    def test_keeps_this_firms_employees_and_remembers_an_unchecked_limit(self):
        form = ImmutableMultiDict([
            ("tool_manage_client_portals", "1"),
            ("tool_limit_manage_client_portals", "1"),
            ("tool_employee_manage_client_portals", "good-id"),
            ("tool_employee_manage_client_portals", "other-firm"),
            ("tool_employee_manage_client_portals", "good-id"),
            ("tool_employee_drafting_notes", "kept-id"),
        ])
        config = appmod._parse_config_from_form(form, {"good-id", "kept-id"})
        portals = config["tool_access"]["manage_client_portals"]
        notes = config["tool_access"]["drafting_notes"]
        self.assertTrue(config["tools_enabled"]["manage_client_portals"])
        self.assertFalse(config["tools_enabled"]["drafting_notes"])
        self.assertTrue(portals["limited"])
        self.assertEqual(portals["employee_ids"], ["good-id"])
        self.assertFalse(notes["limited"])
        self.assertEqual(notes["employee_ids"], ["kept-id"])
        self.assertEqual(set(config["tool_access"]), set(appmod._TOOL_KEYS))

    def test_new_firm_drops_posted_employee_ids(self):
        form = ImmutableMultiDict([
            ("tool_limit_design_questionnaire", "1"),
            ("tool_employee_design_questionnaire", "someone"),
        ])
        config = appmod._parse_config_from_form(form)
        entry = config["tool_access"]["design_questionnaire"]
        self.assertTrue(entry["limited"])
        self.assertEqual(entry["employee_ids"], [])

    def test_employee_login_required_while_any_tool_is_limited(self):
        config = {"tool_access": {
            "manage_client_portals": {"limited": True, "employee_ids": []},
            "drafting_notes": {"limited": False, "employee_ids": ["a"]},
        }}
        self.assertIsNone(appmod._employee_limit_error(config, True))
        message = appmod._employee_limit_error(config, False)
        self.assertIn("Manage Client Portals", message)
        self.assertNotIn("Drafting Notes", message)


class SessionGateTest(unittest.TestCase):
    def _config(self):
        return {
            "tools_enabled": {
                "manage_client_portals": True,
                "estate_tax_calc": True,
                "drafting_notes": False,
            },
            "tool_access": {
                "manage_client_portals": {"limited": True, "employee_ids": ["ada"]},
                "estate_tax_calc": {"limited": False, "employee_ids": []},
                "drafting_notes": {"limited": True, "employee_ids": ["ada"]},
            },
        }

    def test_dashboard_and_direct_link_use_the_same_rule(self):
        config = self._config()
        with appmod.app.test_request_context():
            appmod.session["firm_id"] = "firm"
            appmod.session["employee_id"] = "ada"
            appmod.session["authenticated"] = True
            with patch.object(appmod, "_get_firm_config", return_value=config), \
                 patch.object(appmod, "_firm_requires_employee_login", return_value=True):
                self.assertTrue(appmod._session_may_open_tool("manage_client_portals"))
                self.assertTrue(appmod._session_may_open_tool("estate_tax_calc"))
                self.assertFalse(appmod._session_may_open_tool("drafting_notes"))
                self.assertFalse(appmod._session_may_open_tool("design_questionnaire"))
                self.assertTrue(appmod._session_may_open_tool("doc_separator"))
                appmod.session["employee_id"] = "grace"
                self.assertFalse(appmod._session_may_open_tool("manage_client_portals"))

    def test_page_and_api_refusal(self):
        config = self._config()
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["firm_id"] = "firm"
            sess["employee_id"] = "grace"
        with patch.object(appmod, "_get_firm_config", return_value=config), \
             patch.object(appmod, "_firm_requires_employee_login", return_value=True), \
             patch.object(appmod, "log_tool_use") as log:
            page = client.get("/manage-client-portals")
            self.assertEqual(page.status_code, 302)
            self.assertIn("notice=login", page.headers["Location"])
            api = client.post("/api/estate-tax-calc/use")
            self.assertEqual(api.status_code, 204)
            log.assert_called_once()
            denied = client.post("/api/ep-extract")
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.get_json()["error"], "This tool is not enabled for your firm.")
            log.assert_called_once()

    def test_saved_employee_uuid_stays_checked(self):
        eid = uuid.UUID("11111111-1111-1111-1111-111111111111")
        firm = {
            "id": "firm-1",
            "name": "Smith Law",
            "slug": "smith-law",
            "require_employee_login": False,
            "employee_login_hint": "",
            "usage_dashboard_enabled": False,
            "usage_dashboard_show_costs": False,
            "usage_dashboard_password_plain": "",
            "feedback_widget_enabled": False,
            "doc_separator_redo_log_enabled": False,
            "config": {
                "tools_enabled": {"manage_client_portals": True},
                "tool_access": {
                    "manage_client_portals": {"limited": True, "employee_ids": [str(eid)]},
                },
            },
        }
        employees = [{"id": eid, "name": "Ada Lovelace", "employee_id_code": "AL"}]
        with appmod.app.test_request_context():
            html = appmod.render_template(
                "admin_firm_edit.html", firm=firm, employees=employees,
            )
        self.assertIn(
            'name="tool_employee_manage_client_portals" value="11111111-1111-1111-1111-111111111111" checked',
            html,
        )
        self.assertIn('name="tool_limit_manage_client_portals" value="1" data-limit-toggle checked', html)
        self.assertNotIn("disabled", html.split('name="tool_employee_manage_client_portals"')[1].split(">")[0])
