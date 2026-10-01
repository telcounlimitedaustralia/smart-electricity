import unittest
import sqlite3
from datetime import datetime, timedelta

from rolling_planner import (
    DailyAction,
    PlannerConfig,
    compare_strategies,
    effective_solar_start,
    is_safe,
    optimise,
    overnight_requirement_kwh,
    simulate,
)
from historical_backtest import hourly_actuals


def hourly_days(days):
    """Create deliberately simple hourly forecast scenarios for planner tests."""
    rows = []
    for day, solar_total, load_total in days:
        daylight = range(8, 17)
        for hour in range(24):
            rows.append({
                "timestamp": (day + timedelta(hours=hour)).isoformat(),
                "day": day.date().isoformat(),
                "hour": hour,
                "solar_kwh": solar_total / len(daylight) if hour in daylight else 0.0,
                "load_kwh": load_total / 24.0,
            })
    return rows


def import_tariff(ts):
    if 10 <= ts.hour < 14:
        return 11.11, "cheap"
    if 16 <= ts.hour < 20:
        return 42.0, "expensive"
    return 32.12, "normal"


def export_tariff(ts):
    return (28.0, "premium") if 17 <= ts.hour < 21 else (3.0, "normal")


class RollingPlannerTests(unittest.TestCase):
    def setUp(self):
        self.config = PlannerConfig(action_step_kwh=5.0)

    def test_effective_solar_start_uses_forecast_not_fixed_clock(self):
        hours = hourly_days([(datetime(2026, 10, 3), 18.0, 18.0)])
        self.assertEqual(effective_solar_start(hours, "2026-10-03"), 8)

    def test_overnight_requirement_includes_to_next_meaningful_solar(self):
        hours = hourly_days([
            (datetime(2026, 10, 3), 20.0, 24.0),
            (datetime(2026, 10, 4), 20.0, 24.0),
        ])
        requirement = overnight_requirement_kwh(hours, "2026-10-03", self.config)
        self.assertGreater(requirement, 0.0)
        self.assertLess(requirement, 24.0)

    def test_safety_floor_blocks_over_export(self):
        hours = hourly_days([(datetime(2026, 10, 3), 0.0, 20.0)])
        result = simulate(
            hours,
            start_energy_kwh=42.0,
            actions={"2026-10-03": DailyAction(premium_export_kwh=35.0)},
            config=self.config,
            import_rate_fn=import_tariff,
            export_rate_fn=export_tariff,
        )
        self.assertGreaterEqual(
            result.daily["2026-10-03"]["min_energy_kwh"],
            self.config.absolute_min_kwh,
        )
        baseline = simulate(
            hours,
            start_energy_kwh=42.0,
            config=self.config,
            import_rate_fn=import_tariff,
            export_rate_fn=export_tariff,
        )
        self.assertFalse(is_safe(result, baseline, self.config))

    def test_strategy_comparison_reports_costs_revenue_and_safety(self):
        hours = hourly_days([
            (datetime(2026, 10, 3), 48.0, 20.0),
            (datetime(2026, 10, 4), 12.0, 24.0),
        ])
        comparison = compare_strategies(
            hours,
            start_energy_kwh=42.0,
            strategies={
                "conservative": {"2026-10-03": DailyAction(0.0, 5.0)},
                "aggressive": {"2026-10-03": DailyAction(0.0, 30.0)},
                "hybrid": {
                    "2026-10-03": DailyAction(0.0, 10.0),
                    "2026-10-04": DailyAction(10.0, 0.0),
                },
            },
            config=self.config,
            import_rate_fn=import_tariff,
            export_rate_fn=export_tariff,
        )
        self.assertEqual(set(comparison), {
            "no_action", "conservative", "aggressive", "hybrid"
        })
        self.assertGreater(
            comparison["aggressive"]["export_revenue"],
            comparison["conservative"]["export_revenue"],
        )
        self.assertIn("incremental_value", comparison["hybrid"])

    def test_historical_adapter_integrates_real_intervals_into_hour_bins(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE foxess_history (
                timestamp TEXT PRIMARY KEY, pv_kw REAL, load_kw REAL,
                battery_soc REAL
            )
        """)
        conn.executemany(
            "INSERT INTO foxess_history VALUES (?, ?, ?, ?)",
            [
                ("2026-10-03T08:55:00+10:00", 1.0, 2.0, 50.0),
                ("2026-10-03T09:00:00+10:00", 1.0, 2.0, 50.0),
                ("2026-10-03T09:05:00+10:00", 1.0, 2.0, 50.0),
            ],
        )
        hours = hourly_actuals(conn, datetime(2026, 10, 3).date(), days=1)
        nine_am = next(item for item in hours if item["hour"] == 9)
        self.assertAlmostEqual(nine_am["solar_kwh"], 1.0 / 12.0)
        self.assertAlmostEqual(nine_am["load_kwh"], 2.0 / 12.0)

    def test_joint_search_can_charge_and_export(self):
        hours = hourly_days([
            (datetime(2026, 10, 3), 18.04, 26.26),
            (datetime(2026, 10, 4), 49.54, 24.0),
        ])
        plan = optimise(
            hours,
            start_energy_kwh=21.0,
            config=self.config,
            import_rate_fn=import_tariff,
            export_rate_fn=export_tariff,
        )
        action = plan["actions"]["2026-10-03"]
        self.assertGreater(action.grid_charge_kwh, 0.0)
        self.assertGreater(action.premium_export_kwh, 0.0)
        self.assertGreater(plan["net_value"], plan["baseline"].net_value)

