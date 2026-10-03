import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("FOXESS_API_KEY", "test-token")

import foxess_joint_auto_execute as controller


def group(start, end, mode="SelfUse", cutoff=10.0):
    return {
        "startHour": start,
        "startMinute": 0,
        "endHour": end,
        "endMinute": 0,
        "workMode": mode,
        "extraParam": {
            "fdSoc": cutoff,
            "fdPwr": 10000.0,
            "maxSoc": 100.0,
            "secondWorkMode": "SelfUse",
            "minSocOnGrid": 10.0,
        },
    }


def plan(**overrides):
    values = {
        "charge_kwh": 10.0,
        "charge_target_soc": 80.0,
        "export_kwh": 12.0,
        "export_cutoff_soc": 35.0,
        "plan_json": json.dumps({
            "daily": {
                "required_post_export_soc": 45.0,
                "required_post_export_energy": 18.9,
            }
        }),
    }
    values.update(overrides)
    return values


class JointControllerTests(unittest.TestCase):
    def setUp(self):
        self.unmanaged = group(1, 2)
        self.legacy = group(17, 21, "ForceDischarge", 20.0)
        self.existing = [self.unmanaged, self.legacy]

    def test_charge_phase_preserves_unmanaged_and_owns_only_charge_window(self):
        desired = controller.desired_groups(self.existing, plan(), "charge", 40.0)

        self.assertEqual(desired[0], self.unmanaged)
        managed = [item for item in desired if controller.managed(item)]
        self.assertEqual(len(managed), 1)
        self.assertEqual(controller.slot(managed[0]), (10, 5, 13, 50))
        self.assertEqual(managed[0]["workMode"], "ForceCharge")
        self.assertEqual(managed[0]["extraParam"]["maxSoc"], 80.0)

    def test_charge_is_not_armed_when_live_soc_already_meets_target(self):
        desired = controller.desired_groups(self.existing, plan(), "charge", 82.0)
        self.assertFalse(any(controller.managed(item) for item in desired))

    def test_disabled_phase_removes_all_owned_periods(self):
        desired = controller.desired_groups(
            self.existing, plan(), "charge", 40.0, enabled=False
        )
        self.assertEqual(desired, [self.unmanaged])

    def test_export_cutoff_never_falls_below_protected_reserve(self):
        desired = controller.desired_groups(self.existing, plan(), "export", 90.0)
        export = next(item for item in desired if controller.managed(item))
        self.assertEqual(controller.slot(export), (17, 5, 20, 50))
        self.assertEqual(export["workMode"], "ForceDischarge")
        self.assertGreaterEqual(export["extraParam"]["fdSoc"], 45.0)

    def test_export_is_not_armed_when_reserve_uses_available_energy(self):
        protected = plan(export_cutoff_soc=60.0, plan_json=json.dumps({
            "daily": {"required_post_export_soc": 70.0}
        }))
        desired = controller.desired_groups(self.existing, protected, "export", 65.0)
        self.assertFalse(any(controller.managed(item) for item in desired))

    def test_watchdog_removes_both_current_and_legacy_owned_periods(self):
        legacy_1705 = group(17, 22, "ForceDischarge", 20.0)
        legacy_1705["startMinute"] = 5
        legacy_1705["endMinute"] = 55
        desired = controller.desired_groups(
            [self.unmanaged, self.legacy, legacy_1705], None, "watchdog", 50.0
        )
        self.assertEqual(desired, [self.unmanaged])

    def test_scheduler_uses_sydney_daylight_saving_time(self):
        summer = datetime(2026, 10, 4, 10, 5, tzinfo=controller.TZ)
        self.assertEqual(summer.utcoffset(), timedelta(hours=11))

    def test_telegram_confirmation_names_verified_window_and_decision(self):
        now = datetime(2026, 10, 4, 9, 55, tzinfo=controller.TZ)
        charge_groups = controller.desired_groups(
            self.existing, plan(), "charge", 40.0
        )
        message = controller.schedule_notification(
            "charge", plan(), 40.0, "written and verified", charge_groups, now
        )
        self.assertIn("IMPORT SCHEDULER SET AND VERIFIED", message)
        self.assertIn("10:05 AM-1:50 PM", message)
        self.assertIn("10.0 kWh", message)

    def test_stale_plan_and_stale_soc_are_rejected(self):
        now = datetime(2026, 10, 4, 9, 55, tzinfo=controller.TZ)
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, "energy.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE economic_plan_actions (
                    plan_date TEXT, created_at TEXT, charge_kwh REAL
                )
            """)
            conn.execute(
                "INSERT INTO economic_plan_actions VALUES (?, ?, ?)",
                ("2026-10-04", (now - timedelta(minutes=21)).isoformat(), 2.0),
            )
            conn.execute("""
                CREATE TABLE foxess_live (
                    id INTEGER PRIMARY KEY, timestamp TEXT, battery_soc REAL
                )
            """)
            conn.execute(
                "INSERT INTO foxess_live(timestamp, battery_soc) VALUES (?, ?)",
                ((now - timedelta(minutes=11)).isoformat(), 50.0),
            )
            conn.commit()
            conn.close()

            with self.assertRaisesRegex(RuntimeError, "plan is stale"):
                controller.latest_plan(now, db_path)
            with self.assertRaisesRegex(RuntimeError, "SOC is stale"):
                controller.latest_soc(now, db_path)

    def test_failed_verification_restores_original_schedule(self):
        desired = controller.desired_groups(self.existing, plan(), "charge", 40.0)
        now = datetime(2026, 10, 4, 9, 55, tzinfo=controller.TZ)
        with patch.object(controller, "backup", return_value="backup.json"), patch.object(
            controller, "write_schedule"
        ) as write, patch.object(
            controller, "verify_schedule", side_effect=[RuntimeError("mismatch"), self.existing]
        ) as verify:
            with self.assertRaisesRegex(RuntimeError, "original schedule restored"):
                controller.apply_schedule("device", {"result": {}}, self.existing, desired, now)

        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args_list[0].args[1], desired)
        self.assertEqual(write.call_args_list[1].args[1], self.existing)
        self.assertEqual(verify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
