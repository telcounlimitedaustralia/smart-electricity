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
        self.assertEqual(controller.slot(managed[0]), (10, 5, 13, 55))
        self.assertEqual(managed[0]["workMode"], "ForceCharge")
        self.assertEqual(managed[0]["extraParam"]["maxSoc"], 62.6)
        self.assertEqual(managed[0]["extraParam"]["fdSoc"], 62.6)

    def test_empty_scheduler_can_create_first_charge_after_cleanup(self):
        desired = controller.desired_groups([], plan(), "charge", 23.0)

        self.assertEqual(len(desired), 1)
        charge = desired[0]
        self.assertEqual(controller.slot(charge), (10, 5, 13, 55))
        self.assertEqual(charge["workMode"], "ForceCharge")
        self.assertEqual(charge["extraParam"]["fdPwr"], 10000.0)
        self.assertEqual(charge["extraParam"]["fdSoc"], 45.6)
        self.assertEqual(charge["extraParam"]["maxSoc"], 45.6)
        self.assertEqual(charge["extraParam"]["secondWorkMode"], "SelfUse")
        self.assertFalse(any(controller.default_self_use(item) for item in desired))

    def test_empty_scheduler_can_create_first_export_after_cleanup(self):
        desired = controller.desired_groups([], plan(), "export", 90.0)

        self.assertEqual(len(desired), 1)
        export = desired[0]
        self.assertEqual(controller.slot(export), (17, 5, 20, 55))
        self.assertEqual(export["workMode"], "ForceDischarge")
        self.assertEqual(export["extraParam"]["fdPwr"], 10000.0)
        self.assertEqual(export["extraParam"]["fdSoc"], 59.9)
        self.assertEqual(export["extraParam"]["secondWorkMode"], "SelfUse")
        self.assertFalse(any(controller.default_self_use(item) for item in desired))

    def test_all_day_self_use_fallback_is_never_sent_with_charge(self):
        fallback = group(0, 23, "SelfUse")
        fallback["endMinute"] = 59
        fallback["isRemainMode"] = True
        desired = controller.desired_groups(
            [fallback, self.unmanaged], plan(), "charge", 40.0
        )

        self.assertFalse(any(controller.default_self_use(item) for item in desired))
        self.assertEqual(desired[0], self.unmanaged)
        self.assertEqual(len([item for item in desired if controller.managed(item)]), 1)

    def test_all_day_self_use_fallback_is_ignored_during_readback(self):
        fallback = group(0, 23, "SelfUse")
        fallback["endMinute"] = 59
        explicit = group(17, 21, "ForceDischarge", 45.0)
        self.assertEqual(
            controller.group_signature([fallback, explicit]),
            controller.group_signature([explicit]),
        )

    def test_disabled_groups_are_not_resent(self):
        disabled = group(17, 21, "ForceDischarge", 45.0)
        disabled["enable"] = False
        self.assertEqual(controller.outbound_groups([disabled]), [])

    def test_charge_target_uses_live_soc_and_meter_side_grid_energy(self):
        desired = controller.desired_groups(self.existing, plan(), "charge", 23.0)
        charge = next(item for item in desired if controller.managed(item))
        self.assertEqual(charge["extraParam"]["maxSoc"], 45.6)
        self.assertEqual(charge["extraParam"]["fdSoc"], 45.6)

    def test_charge_is_not_armed_when_optimizer_requests_no_import(self):
        desired = controller.desired_groups(
            self.existing, plan(charge_kwh=0.0), "charge", 82.0
        )
        self.assertFalse(any(controller.managed(item) for item in desired))

    def test_disabled_phase_removes_all_owned_periods(self):
        desired = controller.desired_groups(
            self.existing, plan(), "charge", 40.0, enabled=False
        )
        self.assertEqual(desired, [self.unmanaged])

    def test_export_cutoff_never_falls_below_protected_reserve(self):
        desired = controller.desired_groups(self.existing, plan(), "export", 90.0)
        export = next(item for item in desired if controller.managed(item))
        self.assertEqual(controller.slot(export), (17, 5, 20, 55))
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
        self.assertIn("10:05 AM-1:55 PM", message)
        self.assertIn("10.0 kWh", message)
        self.assertIn("62.6%", message)

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

    def test_deployment_dry_run_can_select_tomorrows_fresh_snapshot(self):
        now = datetime(2026, 10, 4, 23, 35, tzinfo=controller.TZ)
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, "energy.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE economic_plan_actions (
                    plan_date TEXT, created_at TEXT, charge_kwh REAL
                )
            """)
            conn.executemany(
                "INSERT INTO economic_plan_actions VALUES (?, ?, ?)",
                [
                    # Deliberately use a lexically later timestamp string for
                    # the old row. Selection must follow insertion order, not
                    # MAX(timestamp text), during a no-write deployment check.
                    ("2026-10-04", "2026-10-04T99:00:00+10:00", 0.0),
                    ("2026-10-05", now.isoformat(), 19.0),
                    ("2026-10-06", now.isoformat(), 5.0),
                ],
            )
            conn.commit()
            conn.close()

            selected = controller.latest_plan(
                now,
                db_path,
                use_latest_frozen=True,
            )
            self.assertEqual(selected["plan_date"], "2026-10-05")
            self.assertEqual(selected["charge_kwh"], 19.0)

    def test_live_selection_uses_newest_inserted_plan_for_today(self):
        now = datetime(2026, 10, 5, 0, 42, tzinfo=controller.TZ)
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, "energy.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE economic_plan_actions (
                    plan_date TEXT, created_at TEXT, charge_kwh REAL
                )
            """)
            conn.executemany(
                "INSERT INTO economic_plan_actions VALUES (?, ?, ?)",
                [
                    ("2026-10-05", "2026-10-05T99:00:00+11:00", 1.0),
                    ("2026-10-05", now.isoformat(), 19.0),
                ],
            )
            conn.commit()
            conn.close()

            selected = controller.latest_plan(now, db_path)

            self.assertEqual(selected["created_at"], now.isoformat())
            self.assertEqual(selected["charge_kwh"], 19.0)

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
        self.assertEqual(
            write.call_args_list[1].args[1],
            controller.outbound_groups(self.existing),
        )
        self.assertEqual(verify.call_count, 2)

    def test_cleanup_sends_empty_list_not_all_day_self_use(self):
        fallback = group(0, 23, "SelfUse")
        fallback["endMinute"] = 59
        managed_period = group(17, 20, "ForceDischarge", 45.0)
        managed_period["startMinute"] = 5
        managed_period["endMinute"] = 55
        now = datetime(2026, 10, 5, 21, 0, tzinfo=controller.TZ)

        desired = controller.desired_groups(
            [fallback, managed_period], None, "watchdog", 50.0
        )
        with patch.object(controller, "backup", return_value="backup.json"), patch.object(
            controller, "write_schedule"
        ) as write, patch.object(
            controller, "verify_schedule", return_value=[]
        ):
            controller.apply_schedule(
                "device", {"result": {}}, [fallback, managed_period], desired, now
            )

        self.assertEqual(write.call_args.args[1], [])

    def test_disabled_scheduler_is_a_verified_empty_schedule(self):
        response = {
            "errno": 0,
            "result": {
                "enable": 0,
                "groups": [group(10, 14, "ForceCharge", 62.0)],
            },
        }
        with patch.object(controller, "foxess_post", return_value=response):
            payload, active = controller.read_schedule("device")
            verified = controller.verify_schedule(
                "device", [], attempts=1, wait=0
            )

        self.assertEqual(payload, response)
        self.assertEqual(active, [])
        self.assertEqual(verified, [])

    def test_schedule_verification_ignores_foxess_non_control_fields(self):
        expected = group(17, 21, "ForceDischarge", 45.0)
        actual = json.loads(json.dumps(expected))
        actual["extraParam"]["apiGeneratedValue"] = 123.0
        actual["foxessInternalId"] = "normalised-on-readback"
        self.assertEqual(
            controller.group_signature([expected]),
            controller.group_signature([actual]),
        )

    def test_schedule_verification_rejects_changed_cutoff(self):
        expected = group(17, 21, "ForceDischarge", 45.0)
        actual = json.loads(json.dumps(expected))
        actual["extraParam"]["fdSoc"] = 44.0
        self.assertNotEqual(
            controller.group_signature([expected]),
            controller.group_signature([actual]),
        )

    def test_charge_verification_requires_visible_and_firmware_cutoffs(self):
        expected = group(10, 13, "ForceCharge", 62.7)
        expected["startMinute"] = 5
        expected["endMinute"] = 55
        expected["extraParam"]["maxSoc"] = 62.7
        actual = json.loads(json.dumps(expected))
        actual["extraParam"]["fdSoc"] = 10.0

        self.assertNotEqual(
            controller.group_signature([expected]),
            controller.group_signature([actual]),
        )


if __name__ == "__main__":
    unittest.main()
