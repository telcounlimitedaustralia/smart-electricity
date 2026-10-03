import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FOXESS_API_KEY", "test-token")
os.environ["FOXESS_CONTROL_MODE"] = "joint"

from dashboard import app as dashboard_app
import foxess_joint_auto_execute as controller


STATE = {
    "master_enabled": True,
    "charge_enabled": True,
    "export_enabled": True,
    "updated_at": "2026-10-04T08:00:00+11:00",
    "updated_by": "test",
}


class DashboardControlTests(unittest.TestCase):
    def setUp(self):
        dashboard_app.app.config.update(TESTING=True)
        self.client = dashboard_app.app.test_client()

    def test_status_reports_effective_scheduler_switches(self):
        with patch.object(dashboard_app, "get_switches", return_value=dict(STATE)):
            response = self.client.get("/api/control-switches")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["control_mode"], "joint")
        self.assertTrue(data["effective_charge_enabled"])
        self.assertTrue(data["effective_export_enabled"])

    def test_post_requires_dashboard_confirmation_header(self):
        response = self.client.post(
            "/api/control-switch",
            json={"scope": "charge", "enabled": False},
        )
        self.assertEqual(response.status_code, 403)

    def test_switch_off_persists_gate_then_cleans_foxess_schedule(self):
        stopped = dict(STATE, charge_enabled=False)
        with patch.object(dashboard_app, "set_switch", return_value=stopped) as saved, patch.object(
            controller, "disable_scope", return_value=("written and verified", "backup.json")
        ) as disabled, patch.object(controller, "notify"):
            response = self.client.post(
                "/api/control-switch",
                json={"scope": "charge", "enabled": False},
                headers={"X-Requested-With": "SmartElectricityDashboard"},
            )

        self.assertEqual(response.status_code, 200)
        saved.assert_called_once()
        disabled.assert_called_once_with("charge")
        self.assertIn("verified", response.get_json()["result"])

    def test_switch_on_waits_for_next_fresh_decision(self):
        with patch.object(dashboard_app, "set_switch", return_value=dict(STATE)), patch.object(
            controller, "disable_scope"
        ) as disabled:
            response = self.client.post(
                "/api/control-switch",
                json={"scope": "export", "enabled": True},
                headers={"X-Requested-With": "SmartElectricityDashboard"},
            )

        self.assertEqual(response.status_code, 200)
        disabled.assert_not_called()
        self.assertIn("next fresh", response.get_json()["result"])


if __name__ == "__main__":
    unittest.main()
