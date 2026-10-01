"""Read-only conversion of FoxESS five-minute history into planner backtests."""

from __future__ import annotations

import sqlite3
import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from rolling_planner import (
    DailyAction,
    PlannerConfig,
    compare_strategies,
    optimise,
    simulate,
)


BATTERY_KWH = 42.0
DEFAULT_DB = Path.home() / "smart-electricity" / "data" / "energy.db"
TZ = ZoneInfo("Australia/Sydney")


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hourly_actuals(
    conn: sqlite3.Connection,
    start_day: date,
    days: int = 7,
) -> list[dict]:
    """Integrate real telemetry into hour bins using actual sample intervals.

    Intervals larger than ten minutes are ignored, matching the existing
    `strategy_actuals.integrate` data-quality guard.  The function is read-only.
    """

    if days <= 0:
        raise ValueError("days must be positive")
    start = datetime.combine(start_day, time.min, tzinfo=TZ)
    end = start + timedelta(days=days)
    rows = conn.execute(
        """
        SELECT timestamp, pv_kw, load_kw
        FROM foxess_history
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    bins: dict[datetime, dict[str, float]] = defaultdict(
        lambda: {"solar_kwh": 0.0, "load_kwh": 0.0}
    )
    for current, following in zip(rows, rows[1:]):
        interval_start = _as_datetime(current["timestamp"])
        interval_end = _as_datetime(following["timestamp"])
        if not (0 < (interval_end - interval_start).total_seconds() <= 600):
            continue
        pv_kw = max(0.0, float(current["pv_kw"] or 0.0))
        load_kw = max(0.0, float(current["load_kw"] or 0.0))
        cursor = interval_start
        while cursor < interval_end:
            hour_start = cursor.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            segment_end = min(hour_end, interval_end)
            duration = (segment_end - cursor).total_seconds() / 3600.0
            if start <= hour_start < end:
                bins[hour_start]["solar_kwh"] += pv_kw * duration
                bins[hour_start]["load_kwh"] += load_kw * duration
            cursor = segment_end

    hourly = []
    for offset in range(days * 24):
        timestamp = start + timedelta(hours=offset)
        values = bins[timestamp]
        hourly.append({
            "timestamp": timestamp.isoformat(),
            "day": timestamp.date().isoformat(),
            "hour": timestamp.hour,
            **values,
        })
    return hourly


def start_energy_from_history(
    conn: sqlite3.Connection,
    start_day: date,
    battery_kwh: float = BATTERY_KWH,
) -> float:
    row = conn.execute(
        """
        SELECT battery_soc FROM foxess_history
        WHERE substr(timestamp, 1, 10) = ? AND battery_soc IS NOT NULL
        ORDER BY timestamp LIMIT 1
        """,
        (start_day.isoformat(),),
    ).fetchone()
    if row is None:
        raise ValueError(f"No starting SOC found for {start_day.isoformat()}")
    return battery_kwh * float(row[0]) / 100.0


def replay(
    conn: sqlite3.Connection,
    start_day: date,
    strategies: Mapping[str, Mapping[str, DailyAction]],
    days: int = 7,
    config: PlannerConfig = PlannerConfig(),
) -> dict:
    """Compare named plans against a real seven-day solar/load period."""

    hours = hourly_actuals(conn, start_day, days)
    start_energy = start_energy_from_history(conn, start_day, config.battery_kwh)
    comparison = compare_strategies(hours, start_energy, strategies, config)
    actual_self_consumption = simulate(hours, start_energy, {}, config)
    return {
        "start_day": start_day.isoformat(),
        "days": days,
        "starting_energy_kwh": start_energy,
        "comparison": comparison,
        "historical_no_action": actual_self_consumption,
    }


def main() -> None:
    """Run a read-only no-action replay summary for the latest complete week."""

    parser = argparse.ArgumentParser(description="Read-only historical planner replay")
    parser.add_argument(
        "--optimise",
        action="store_true",
        help="also run the joint seven-day optimiser against historical inputs",
    )
    args = parser.parse_args()

    with sqlite3.connect(DEFAULT_DB) as conn:
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            "SELECT MAX(substr(timestamp, 1, 10)) FROM foxess_history"
        ).fetchone()[0]
        if not latest:
            raise SystemExit("No FoxESS history available")
        end_day = date.fromisoformat(latest)
        start_day = end_day - timedelta(days=7)
        hours = hourly_actuals(conn, start_day)
        start_energy = start_energy_from_history(conn, start_day)
        result = simulate(hours, start_energy)
    print(f"Historical replay: {start_day} for 7 days")
    print(f"Start energy: {start_energy:.2f} kWh")
    print(f"Solar: {sum(row['solar_kwh'] for row in hours):.2f} kWh")
    print(f"Load: {sum(row['load_kwh'] for row in hours):.2f} kWh")
    print(f"No-action net value: ${result.net_value:.2f}")
    if args.optimise:
        plan = optimise(hours, start_energy)
        print(f"Optimised net value: ${plan['net_value']:.2f}")
        print(f"Avoided import cost: ${plan['avoided_import_cost']:.2f}")
        for day, action in plan["actions"].items():
            print(
                f"{day}: charge={action.grid_charge_kwh:.1f} kWh, "
                f"premium_export={action.premium_export_kwh:.1f} kWh"
            )


if __name__ == "__main__":
    main()
