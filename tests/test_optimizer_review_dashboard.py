import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
        self.solar_start_timestamp = datetime.now(
            ZoneInfo("Australia/Sydney")
        ).replace(
            hour=6, minute=30, second=0, microsecond=0
        ).isoformat(timespec="seconds")
        self.weather_date = datetime.now(
            ZoneInfo("Australia/Sydney")
        ).date().isoformat()
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
                "forecast_draw_to_recovery": 10.9,
                "grid_stored": 13.3,
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
        conn.execute(
            "INSERT INTO foxess_live "
            "(timestamp, battery_soc, pv_kw, load_kw, grid_import_kw, grid_export_kw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.solar_start_timestamp, 35.0, 0.4, 1.2, 0.8, 0.0),
        )
        conn.execute("""
            CREATE TABLE weather (
                timestamp TEXT PRIMARY KEY, temperature REAL,
                apparent_temperature REAL, cloud_cover REAL,
                precipitation REAL, wind_speed REAL,
                shortwave_radiation REAL, direct_radiation REAL,
                diffuse_radiation REAL, sunshine_duration REAL,
                source TEXT
            )
        """)
        conn.execute(
            "INSERT INTO weather VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{self.weather_date}T10:00:00+11:00", 19.0, 18.0, 82.0, 0.6,
                8.0, 240.0, 80.0, 160.0, 900.0, "test",
            ),
        )
        conn.execute(
            "INSERT INTO weather VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{self.weather_date}T14:00:00+11:00", 28.6, 27.4, 8.0, 0.0,
                12.0, 935.0, 700.0, 235.0, 3600.0, "test",
            ),
        )
        conn.commit()
        conn.close()

    def test_review_page_is_separate_and_explains_control_truth(self):
        response = self.client.get("/optimizer-review")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("My Battery Plan", body)
        self.assertIn("Current recommendation for today", body)
        self.assertIn("Seven-day plan", body)
        self.assertIn("Forecast versus actual", body)
        self.assertIn("Today’s energy journey", body)
        self.assertIn('id="dayActivity"', body)
        self.assertIn('class="day-source ${source.toLowerCase()}"', body)
        self.assertIn('aria-label="Today’s solar and weather outlook"', body)
        self.assertIn('id="solarForecastValue"', body)
        self.assertIn('id="solarActualValue"', body)
        self.assertIn('id="solarRemainingValue"', body)
        self.assertIn('id="homeUseForecastValue"', body)
        self.assertIn('id="weatherArt"', body)
        self.assertIn('id="weatherTemperature"', body)
        self.assertIn('id="weatherCloud"', body)
        self.assertIn('id="weatherRain"', body)
        self.assertIn('id="weatherRadiation"', body)
        self.assertIn("renderWeatherSummary(audit.weather_summary)", body)
        self.assertIn("renderWeather(audit.weather)", body)
        self.assertIn("gross solar", body)
        self.assertIn("After hourly home use by 5 PM", body)
        self.assertNotIn('class="action-grid"', body)
        self.assertLess(body.index('class="hero"'), body.index('class="quick-status"'))
        self.assertIn("BATTERY_KWH", body)
        self.assertIn("Solar outlook", body)
        self.assertIn("Keep for home", body)
        self.assertIn('class="plan-pct"', body)
        self.assertIn('class="plan-kwh"', body)
        self.assertIn('class="dashboard-section"', body)
        self.assertIn('<details class="comparison-disclosure" id="weekPlanDisclosure">', body)
        self.assertNotIn('<details class="comparison-disclosure" id="weekPlanDisclosure" open>', body)
        self.assertIn('<details class="comparison-disclosure">', body)
        self.assertIn("Forecast versus actual details", body)
        self.assertNotIn("Why today’s plan makes sense", body)
        self.assertNotIn("What the automation will do", body)
        self.assertNotIn("Technical details", body)
        self.assertNotIn('id="automationDetails"', body)
        self.assertNotIn('id="technicalDetails"', body)
        self.assertIn("stopping at", body)
        self.assertIn("Battery energy bridge", body)
        self.assertIn('id="batteryBridge"', body)
        self.assertIn('class="bridge-pct"', body)
        self.assertIn('class="bridge-kwh"', body)
        self.assertIn('id="bridgeReserve"', body)
        self.assertIn('id="bridgeRevenue"', body)
        self.assertIn('id="mlWinRing"', body)
        self.assertIn('id="loadErrorBar"', body)
        self.assertIn('id="solarErrorBar"', body)
        self.assertIn("ML learning performance", body)
        self.assertIn('<details class="comparison-disclosure" id="learningDisclosure">', body)
        self.assertNotIn('<details class="comparison-disclosure" id="learningDisclosure" open>', body)
        self.assertIn('id="learningConfidence"', body)
        self.assertIn('id="learningError"', body)
        self.assertIn('id="learningImprovement"', body)
        self.assertIn("Extra Trees", body)
        self.assertIn("not currently automatic every day", body)
        self.assertIn("Today versus FoxESS fixed 60%", body)
        self.assertIn('id="fixed60Outcome"', body)
        self.assertIn("renderFixed60(day)", body)
        self.assertIn('aria-label="Forecast versus actual chart"', body)
        self.assertIn('aria-label="Forecast versus actual key results"', body)
        self.assertIn('class="compare-pct"', body)
        self.assertIn('class="compare-kwh"', body)
        self.assertNotIn('class="compare-meter"', body)
        self.assertNotIn("Grid import</th>", body)
        self.assertNotIn("Import cost</th>", body)
        self.assertNotIn("Net result</th>", body)
        self.assertIn('id="chartMetricLabel"', body)
        self.assertLess(body.index('id="batteryBridge"'), body.index('id="performanceChart"'))
        self.assertLess(body.index('id="learningHeading"'), body.index('id="performanceChart"'))
        self.assertGreater(body.index('id="performanceChart"'), body.index("Seven-day plan"))
        self.assertIn("lower is better", body)
        self.assertNotIn("Morning battery", body)

    def test_fixed_60_comparison_uses_same_5pm_battery_and_efficiency(self):
        result = dashboard_app.fixed_60_comparison(42.0, 19.0, 52.4)

        self.assertEqual(result["fixed_cutoff_soc"], 60.0)
        self.assertEqual(result["fixed_export_kwh"], 15.96)
        self.assertEqual(result["fixed_export_revenue"], 4.47)
        self.assertEqual(result["optimiser_export_revenue"], 5.32)
        self.assertEqual(result["extra_export_kwh"], 3.04)
        self.assertEqual(result["extra_revenue"], 0.85)

    def test_wattsiq_is_public_copy_without_operator_navigation(self):
        response = self.client.get("/wattsiq")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<title>WattsIQ</title>", body)
        self.assertIn("<h1>WattsIQ</h1>", body)
        self.assertNotIn(">Main dashboard</a>", body)
        self.assertIn('const API_PREFIX="/wattsiq/api";', body)

    def test_wattsiq_api_is_get_only_and_allowlisted(self):
        with patch.object(dashboard_app, "DB", self.db_path), patch.object(
            dashboard_app, "get_switches", return_value=dict(SWITCHES)
        ):
            allowed = self.client.get("/wattsiq/api/optimizer-audit")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.client.get("/wattsiq/api/control-switch").status_code, 404)
        self.assertEqual(self.client.post("/wattsiq/api/optimizer-audit").status_code, 405)

    def test_audit_api_returns_frozen_plan_live_state_and_execution(self):
        with patch.object(dashboard_app, "DB", self.db_path), patch.object(
            dashboard_app, "get_switches", return_value=dict(SWITCHES)
        ):
            response = self.client.get("/api/optimizer-audit")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["available"])
        self.assertEqual(data["frozen"]["created_at"], "2026-10-05T09:45:00+11:00")
        self.assertFalse(data["frozen"]["is_final_export_plan"])
        self.assertEqual(data["frozen"]["days"][0]["charge_kwh"], 14.0)
        self.assertEqual(data["frozen"]["days"][0]["export_kwh"], 21.0)
        self.assertAlmostEqual(
            data["frozen"]["days"][0]["expected_post_export_soc"],
            17.0 / 42.0 * 100.0,
        )
        self.assertEqual(data["frozen"]["days"][0]["solar_only_5pm_soc"], 67.7)
        self.assertEqual(data["frozen"]["days"][0]["full_day_solar_kwh"], 32.5)
        self.assertIsNone(data["frozen"]["days"][0]["actual_solar_so_far_kwh"])
        self.assertEqual(data["frozen"]["days"][0]["remaining_solar_kwh"], 32.5)
        self.assertEqual(data["frozen"]["days"][0]["stored_from_grid_kwh"], 13.3)
        self.assertEqual(
            data["frozen"]["days"][0]["forecast_draw_to_recharge_kwh"],
            10.9,
        )
        self.assertEqual(
            data["frozen"]["days"][0]["solar_basis"],
            "FULL_DAY_PROTECTED_FORECAST",
        )
        self.assertEqual(data["live"]["battery_soc"], 35.0)
        self.assertIn(data["weather"]["cloud_cover"], (82.0, 8.0))
        self.assertIn(data["weather"]["precipitation"], (0.6, 0.0))
        self.assertEqual(data["weather_summary"]["temperature_min"], 19.0)
        self.assertEqual(data["weather_summary"]["temperature_max"], 28.6)
        self.assertEqual(data["weather_summary"]["cloud_cover_daylight"], 45.0)
        self.assertEqual(data["weather_summary"]["rain_total"], 0.6)
        self.assertEqual(data["weather_summary"]["peak_radiation"], 935.0)
        self.assertEqual(
            data["live"]["solar_start_timestamp"],
            self.solar_start_timestamp,
        )
        self.assertEqual(data["live"]["solar_start_soc"], 35.0)
        self.assertAlmostEqual(data["live"]["solar_start_kwh"], 14.7)
        self.assertEqual(data["events"][0]["status"], "verified")
        self.assertEqual(data["control"]["schedule"]["import_window"], "10:05-13:55")


if __name__ == "__main__":
    unittest.main()
