import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import economic_optimizer_v2 as optimiser


class JointOptimisationTests(unittest.TestCase):
    def test_control_candidate_search_includes_exact_shortened_window_limit(self):
        maximum = optimiser.MAX_GRID_CHARGE_KW * optimiser.CONTROL_WINDOW_HOURS
        self.assertEqual(optimiser.candidate_values(maximum)[-1], 38.33)

    def test_manual_grid_charge_is_excluded_from_solar_only_replay(self):
        rows = [
            {
                "timestamp": "2026-10-03T12:35:00+10:00",
                "battery_soc": 68.0,
                "pv_kw": 4.0,
                "load_kw": 0.3,
                "grid_import_kw": 6.0,
                "battery_charge_kw": 10.0,
            },
            {
                "timestamp": "2026-10-03T12:40:00+10:00",
                # The real SOC includes the manual grid charge.  The replay
                # must not use this later SOC as a new starting point.
                "battery_soc": 72.0,
                "pv_kw": 4.0,
                "load_kw": 0.3,
                "grid_import_kw": 6.0,
                "battery_charge_kw": 10.0,
            },
        ]

        result = optimiser.replay_without_grid_charge(rows)
        expected_energy = (
            optimiser.BATTERY_KWH * 0.68
            + 2 * (4.0 - 0.3) / 12.0 * optimiser.CHARGE_EFF
        )

        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["energy"], expected_energy, places=4)
        self.assertAlmostEqual(result["grid_charge_ac"], 1.0, places=4)
        self.assertAlmostEqual(result["grid_stored"], 0.95, places=4)

    def test_solar_only_replay_never_exceeds_battery_capacity(self):
        rows = [
            {
                "timestamp": "2026-10-03T14:00:00+10:00",
                "battery_soc": 99.0,
                "pv_kw": 9.0,
                "load_kw": 0.2,
                "grid_import_kw": 0.0,
                "battery_charge_kw": 8.8,
            }
        ]

        result = optimiser.replay_without_grid_charge(rows)

        self.assertEqual(result["energy"], optimiser.BATTERY_KWH)
        self.assertEqual(result["grid_charge_ac"], 0.0)

    def test_grid_charge_is_zero_when_solar_alone_fills_battery(self):
        start = datetime(2026, 10, 3)
        hours = []
        for hour in range(24):
            hours.append({
                "timestamp": (start + timedelta(hours=hour)).isoformat(),
                "day": "2026-10-03",
                "hour": hour,
                "solar_kwh": 5.0 if 14 <= hour < 17 else 0.0,
                "load_kwh": 0.0,
            })

        result = optimiser.simulate(
            hours,
            start_energy=optimiser.BATTERY_KWH * 0.70,
            charges={"2026-10-03": 40.0},
            exports={"2026-10-03": 0.0},
        )

        day = result["daily"]["2026-10-03"]
        self.assertAlmostEqual(day["solar_only_5pm_energy"], optimiser.BATTERY_KWH)
        self.assertAlmostEqual(day["grid_charge"], 0.0)

    def test_grid_charge_only_closes_solar_only_shortfall(self):
        start = datetime(2026, 10, 3)
        hours = []
        # Starting at 30%, the post-shoulder solar stores 40 percentage points,
        # so solar alone reaches 70% and grid charging may supply only the 30%
        # battery shortfall (12.6 kWh stored on a 42 kWh battery).
        solar_ac = (optimiser.BATTERY_KWH * 0.40) / optimiser.CHARGE_EFF
        for hour in range(24):
            hours.append({
                "timestamp": (start + timedelta(hours=hour)).isoformat(),
                "day": "2026-10-03",
                "hour": hour,
                "solar_kwh": solar_ac if hour == 15 else 0.0,
                "load_kwh": 0.0,
            })

        result = optimiser.simulate(
            hours,
            start_energy=optimiser.BATTERY_KWH * 0.30,
            charges={"2026-10-03": 40.0},
            exports={"2026-10-03": 0.0},
        )

        day = result["daily"]["2026-10-03"]
        expected_import = (optimiser.BATTERY_KWH * 0.30) / optimiser.CHARGE_EFF
        self.assertAlmostEqual(day["solar_only_5pm_soc"], 70.0, places=3)
        self.assertAlmostEqual(day["grid_charge"], expected_import, places=3)
        self.assertAlmostEqual(day["pre_export_energy"], optimiser.BATTERY_KWH)

    def test_hot_night_reserve_extends_to_next_10am_shoulder(self):
        first = datetime(2026, 10, 3)
        second = datetime(2026, 10, 4)
        hours = []
        for start in (first, second):
            for hour in range(24):
                load = 0.0
                if start == first and hour >= 21:
                    load = 2.0
                if start == second and hour < 10:
                    load = 2.0
                hours.append({
                    "timestamp": (start + timedelta(hours=hour)).isoformat(),
                    "day": start.date().isoformat(),
                    "hour": hour,
                    "solar_kwh": 0.0,
                    "load_kwh": load,
                })

        result = optimiser.simulate(
            hours,
            start_energy=optimiser.BATTERY_KWH,
            charges={},
            exports={},
        )

        day = result["daily"]["2026-10-03"]
        forecast_load = 26.0
        expected = optimiser.MIN_KWH + forecast_load / optimiser.DISCHARGE_EFF
        self.assertAlmostEqual(day["required_post_export_energy"], expected, places=3)
        self.assertEqual(day["next_recharge_type"], "SHOULDER_10AM")
        self.assertTrue(day["next_recharge_timestamp"].startswith("2026-10-04T10:"))

    def test_battery_side_values_include_losses_wear_and_terminal_value(self):
        start = datetime(2026, 10, 3)
        hours = [
            {
                "timestamp": (start + timedelta(hours=hour)).isoformat(),
                "day": "2026-10-03",
                "hour": hour,
                "solar_kwh": 0.0,
                "load_kwh": 0.0,
            }
            for hour in range(24)
        ]

        def import_rate(ts):
            return (11.11 if 10 <= ts.hour < 14 else 32.12, "test")

        def export_rate(ts):
            return (28.0 if 17 <= ts.hour < 21 else 3.0, "test")

        with patch.object(optimiser, "import_rate", side_effect=import_rate), patch.object(
            optimiser, "export_rate", side_effect=export_rate
        ):
            result = optimiser.simulate(
                hours,
                start_energy=21.0,
                charges={"2026-10-03": 10.0},
                exports={"2026-10-03": 5.0},
            )

        day = result["daily"]["2026-10-03"]
        self.assertAlmostEqual(day["grid_stored"], 9.5, places=4)
        self.assertAlmostEqual(
            day["premium_export_battery_draw"],
            5.0 / optimiser.DISCHARGE_EFF,
            places=4,
        )
        self.assertGreater(result["degradation_cost"], 0.0)
        self.assertGreater(result["planning_value"], result["net_value"])

    def test_joint_search_crosses_charge_export_coordination_trap(self):
        """Charge-only loses money and export-only is unsafe; both win."""
        start = datetime(2026, 10, 3)
        hours = [
            {
                "timestamp": (start + timedelta(hours=hour)).isoformat(),
                "day": "2026-10-03",
                "hour": hour,
                "solar_kwh": 0.0,
                "load_kwh": 0.0,
            }
            for hour in range(24)
        ]

        start_energy = optimiser.MIN_KWH + optimiser.TERMINAL_RESERVE_KWH

        def import_rate(ts):
            return (11.11 if 10 <= ts.hour < 14 else 42.0, "test")

        def export_rate(ts):
            return (28.0 if 17 <= ts.hour < 21 else 5.0, "test")

        with patch.object(optimiser, "import_rate", side_effect=import_rate), patch.object(
            optimiser, "export_rate", side_effect=export_rate
        ):
            result = optimiser.optimise_horizon(hours, start_energy, max_passes=2)

        day = "2026-10-03"
        self.assertEqual(result["baseline_exports"][day], 0.0)
        self.assertGreater(result["charges"][day], 0.0)
        self.assertGreater(result["exports"][day], 0.0)
        self.assertGreater(result["final_score"], result["baseline_score"])
        self.assertTrue(
            optimiser.result_is_safe(
                result["final_result"],
                [day],
                baseline_pre10={day: 0.0},
            )
        )

    def test_oct3_low_solar_then_strong_oct4_values_charge_and_export_together(self):
        """Acceptance case: 18.04kWh solar then 49.54kWh next day."""
        days = [
            (datetime(2026, 10, 3), 18.04, 26.26),
            (datetime(2026, 10, 4), 49.54, 24.00),
        ]
        hours = []
        for start, solar_total, load_total in days:
            daylight = list(range(7, 18))
            for hour in range(24):
                hours.append({
                    "timestamp": (start + timedelta(hours=hour)).isoformat(),
                    "day": start.date().isoformat(),
                    "hour": hour,
                    "solar_kwh": solar_total / len(daylight) if hour in daylight else 0.0,
                    "load_kwh": load_total / 24.0,
                })

        def import_rate(ts):
            if 10 <= ts.hour < 14:
                return 11.11, "cheap"
            if 17 <= ts.hour < 21:
                return 42.0, "peak"
            return 32.12, "standard"

        def export_rate(ts):
            return (28.0 if 17 <= ts.hour < 21 else 5.0), "export"

        with patch.object(optimiser, "import_rate", side_effect=import_rate), patch.object(
            optimiser, "export_rate", side_effect=export_rate
        ):
            result = optimiser.optimise_horizon(
                hours,
                optimiser.BATTERY_KWH * 0.50,
                max_passes=2,
            )

        oct3 = "2026-10-03"
        self.assertGreater(result["charges"][oct3], 0.0)
        self.assertGreater(result["exports"][oct3], 0.0)
        self.assertGreater(result["final_score"], result["baseline_score"])
        self.assertGreaterEqual(
            result["final_result"]["daily"][oct3]["min_energy"],
            optimiser.MIN_KWH,
        )


if __name__ == "__main__":
    unittest.main()
