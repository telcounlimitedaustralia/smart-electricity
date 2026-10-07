import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import economic_plan_snapshot as snapshot


class EconomicPlanSnapshotTests(unittest.TestCase):
    def test_empty_forecast_is_not_reported_as_a_frozen_plan(self):
        now = datetime(2026, 10, 5, 0, 41, tzinfo=snapshot.TZ)
        with patch.object(snapshot.optimiser, "build_hours", return_value=([], {})):
            with self.assertRaisesRegex(RuntimeError, "no joint plan was saved"):
                snapshot.build_plan(now=now)

    def test_plan_write_waits_for_brief_collector_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, "energy.db")
            ready = threading.Event()

            def collector_write():
                conn = sqlite3.connect(db_path)
                conn.execute("CREATE TABLE IF NOT EXISTS telemetry (value INTEGER)")
                conn.commit()
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute("INSERT INTO telemetry VALUES (1)")
                ready.set()
                time.sleep(0.2)
                conn.commit()
                conn.close()

            collector = threading.Thread(target=collector_write)
            collector.start()
            self.assertTrue(ready.wait(timeout=2))

            def save(conn):
                conn.execute("CREATE TABLE plan (value INTEGER)")
                conn.execute("INSERT INTO plan VALUES (7)")

            snapshot.write_with_retry(save, db_path)
            collector.join(timeout=2)

            conn = sqlite3.connect(db_path)
            value = conn.execute("SELECT value FROM plan").fetchone()[0]
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()

            self.assertEqual(value, 7)
            self.assertEqual(mode.lower(), "wal")

    def test_late_day_plan_uses_live_fallbacks_and_remaining_actions(self):
        now = datetime(2026, 10, 6, 19, 40, tzinfo=snapshot.TZ)
        day = "2026-10-06"
        ml = {
            day: {
                "predicted_load_kwh": 20.0,
                "safe_load_kwh": 24.0,
            }
        }
        daily = {
            "solar": 1.0,
            "start_energy": 20.0,
            "post_shoulder_energy": None,
            "pre_export_energy": None,
            "post_export_energy": 17.0,
            "end_energy": 16.0,
            "grid_charge": 0.0,
            "premium_export": 4.0,
            "grid_import": 0.0,
        }
        result = {
            "days": [day],
            "charges": {day: 14.0},
            "exports": {day: 21.0},
            "final_score": 2.0,
            "baseline_score": 1.0,
            "final_result": {"daily": {day: daily}},
        }

        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "energy.db"

            def write_to_fixture(callback):
                conn = sqlite3.connect(db_path)
                callback(conn)
                conn.commit()
                conn.close()

            with patch.object(snapshot.optimiser, "build_hours", return_value=([{}], ml)), patch.object(
                snapshot.optimiser, "initial_energy", return_value=(20.0, 47.6)
            ), patch.object(
                snapshot.optimiser, "optimise_horizon", return_value=result
            ), patch.object(snapshot, "write_with_retry", side_effect=write_to_fixture):
                snapshot.build_plan(now=now)

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT charge_kwh, charge_target_soc, expected_5pm_soc, "
                "export_kwh, export_cutoff_soc FROM economic_plan_actions"
            ).fetchone()
            conn.close()

        self.assertEqual(row[0], 0.0)
        self.assertAlmostEqual(row[1], 20.0 / snapshot.optimiser.BATTERY_KWH * 100.0)
        self.assertAlmostEqual(row[2], 20.0 / snapshot.optimiser.BATTERY_KWH * 100.0)
        self.assertEqual(row[3], 4.0)
        expected_cutoff = (
            20.0 - 4.0 / snapshot.optimiser.DISCHARGE_EFF
        ) / snapshot.optimiser.BATTERY_KWH * 100.0
        self.assertAlmostEqual(row[4], expected_cutoff)


if __name__ == "__main__":
    unittest.main()
