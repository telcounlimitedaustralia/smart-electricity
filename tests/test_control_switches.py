import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
