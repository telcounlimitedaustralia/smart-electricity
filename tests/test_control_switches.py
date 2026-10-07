import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime

from control_switches import get_switches, phase_enabled, set_all, set_switch, TZ


class ControlSwitchTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.folder.name, "energy.db")

    def tearDown(self):
        self.folder.cleanup()

    def test_safe_default_is_everything_off(self):
        state = get_switches(self.db_path)
        self.assertFalse(state["master_enabled"])
        self.assertFalse(state["charge_enabled"])
        self.assertFalse(state["export_enabled"])

    def test_master_and_individual_switches_gate_each_phase(self):
        now = datetime(2026, 10, 4, 8, 0, tzinfo=TZ)
        state = set_all(True, "test", "enable", self.db_path, now)
        self.assertTrue(phase_enabled(state, "charge"))
        self.assertTrue(phase_enabled(state, "export"))

        state = set_switch("charge", False, "test", "stop", self.db_path, now)
        self.assertFalse(phase_enabled(state, "charge"))
        self.assertTrue(phase_enabled(state, "export"))

        state = set_switch("master", False, "test", "stop all", self.db_path, now)
        self.assertFalse(phase_enabled(state, "charge"))
        self.assertFalse(phase_enabled(state, "export"))

    def test_invalid_scope_and_non_boolean_are_rejected(self):
        with self.assertRaises(ValueError):
            set_switch("unknown", False, db_path=self.db_path)
        with self.assertRaises(ValueError):
            set_switch("charge", 1, db_path=self.db_path)

    def test_read_does_not_request_write_lock_during_collector_transaction(self):
        expected = set_all(True, "test", "enable", self.db_path)
        writer = sqlite3.connect(self.db_path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            actual = get_switches(self.db_path)
            elapsed = time.monotonic() - started
        finally:
            writer.rollback()
            writer.close()

        self.assertEqual(actual, expected)
        self.assertLess(elapsed, 1.0)

    def test_set_all_uses_one_atomic_transaction_and_records_each_scope(self):
        now = datetime(2026, 10, 7, 16, 30, tzinfo=TZ)
        state = set_all(False, "deployment", "controller deployment", self.db_path, now)

        self.assertFalse(state["master_enabled"])
        self.assertFalse(state["charge_enabled"])
        self.assertFalse(state["export_enabled"])
        conn = sqlite3.connect(self.db_path)
        try:
            events = conn.execute(
                "SELECT scope, enabled FROM control_switch_events ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(events, [("charge", 0), ("export", 0), ("master", 0)])


if __name__ == "__main__":
    unittest.main()
