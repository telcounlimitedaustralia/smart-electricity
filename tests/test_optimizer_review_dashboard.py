import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("FOXESS_API_KEY", "test-token")
os.environ["FOXESS_CONTROL_MODE"] = "joint"

from dashboard import app as dashboard_app


SWITCHES = {
    "master_enabled": True,
    "charge_enabled": True,
    "export_enabled": True,
    "updated_at": "2026-10-05T08:00:00+11:00",
    "updated_by": "test",
}


class OptimizerReviewDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "energy.db"
        self._create_fixture_database()
        dashboard_app.app.config.update(TESTING=True)
        self.client = dashboard_app.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_fixture_database(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE economic_plan_actions (
                plan_date TEXT NOT NULL, created_at TEXT NOT NULL,
                model_version TEXT NOT NULL, solar_kwh REAL NOT NULL,
                point_load_kwh REAL NOT NULL, safe_load_kwh REAL NOT NULL,
                charge_kwh REAL NOT NULL, charge_target_soc REAL NOT NULL,
                expected_5pm_soc REAL NOT NULL, export_kwh REAL NOT NULL,
                export_cutoff_soc REAL NOT NULL, expected_end_soc REAL NOT NULL,
                grid_import_kwh REAL NOT NULL, net_value REAL NOT NULL,
                baseline_value REAL NOT NULL, reason TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                PRIMARY KEY (plan_date, created_at)
            )
        """)
        payload = {
            "start_soc": 31.0,
            "daily": {
                "required_post_export_soc": 36.0,
                "required_post_export_energy": 15.1,
                "post_export_energy": 17.0,
                "solar_only_5pm_soc": 67.7,
                "pre_10am_grid_import": 0.0,
                "next_recharge_timestamp": "2026-10-06T09:00:00+11:00",
                "next_recharge_type": "solar",
                "import_cost": 1.56,
                "export_revenue": 5.88,
                "degradation_cost": 0.42,
            },
        }
        conn.execute(
            "INSERT INTO economic_plan_actions VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-10-05", "2026-10-05T09:45:00+11:00",
                "load_candidate_v2/calibrated-safe-v1", 32.5, 20.9, 26.5,
                14.0, 62.7, 100.0, 21.0, 50.0, 29.0, 14.0,
                4.32, 2.47, "Buy cheap energy; export only the safe surplus.",
                json.dumps(payload),
            ),
        )
        conn.execute("""
            CREATE TABLE automation_events (
                event_time TEXT NOT NULL, plan_date TEXT NOT NULL,
                phase TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                charge_kwh REAL, export_kwh REAL,
                PRIMARY KEY (event_time, phase)
            )
        """)
        conn.execute(
            "INSERT INTO automation_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-10-05T09:55:02+11:00", "2026-10-05", "charge",
                "verified", "FoxESS charge schedule verified", 14.0, 21.0,
            ),
        )
        conn.execute("""
            CREATE TABLE foxess_live (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                battery_soc REAL, pv_kw REAL, load_kw REAL,
                grid_import_kw REAL, grid_export_kw REAL
            )
        """)
        conn.execute(
            "INSERT INTO foxess_live "
            "(timestamp, battery_soc, pv_kw, load_kw, grid_import_kw, grid_export_kw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-10-05T10:10:00+11:00", 35.0, 4.2, 1.3, 8.4, 0.0),
        )
        conn.commit()
        conn.close()

    def test_review_page_is_separate_and_explains_control_truth(self):
        response = self.client.get("/optimizer-review")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("My Battery Plan", body)
        self.assertIn("What will happen today?", body)
        self.assertIn("Seven-day plan", body)
        self.assertIn("Show technical details", body)
        self.assertIn("predicted FoxESS selling cutoff", body)

    def test_audit_api_returns_frozen_plan_live_state_and_execution(self):
        with patch.object(dashboard_app, "DB", self.db_path), patch.object(
            dashboard_app, "get_switches", return_value=dict(SWITCHES)
        ):
            response = self.client.get("/api/optimizer-audit")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["available"])
        self.assertEqual(data["frozen"]["created_at"], "2026-10-05T09:45:00+11:00")
        self.assertEqual(data["frozen"]["days"][0]["charge_kwh"], 14.0)
        self.assertEqual(data["frozen"]["days"][0]["export_kwh"], 21.0)
        self.assertAlmostEqual(
            data["frozen"]["days"][0]["expected_post_export_soc"],
            17.0 / 42.0 * 100.0,
        )
        self.assertEqual(data["frozen"]["days"][0]["solar_only_5pm_soc"], 67.7)
        self.assertEqual(data["live"]["battery_soc"], 35.0)
        self.assertEqual(data["events"][0]["status"], "verified")
        self.assertEqual(data["control"]["schedule"]["import_window"], "10:05-13:55")


if __name__ == "__main__":
    unittest.main()
