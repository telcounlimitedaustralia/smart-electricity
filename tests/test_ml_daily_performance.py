import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ml_daily_performance import calculate


def test_forecast_audit_compares_matching_premium_window(tmp_path):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE strategy_validation (
            plan_date TEXT,
            forecast_solar_kwh REAL,
            forecast_load_kwh REAL,
            ml_load_forecast_kwh REAL,
            ml_safe_load_kwh REAL,
            actual_solar_kwh REAL,
            actual_load_kwh REAL,
            recommended_export_kwh REAL,
            predicted_grid_import_kwh REAL,
            predicted_import_cost REAL,
            predicted_export_revenue REAL,
            predicted_net_value REAL,
            actual_grid_import_kwh REAL,
            actual_grid_export_kwh REAL,
            actual_import_cost REAL,
            actual_export_revenue REAL,
            actual_net_value REAL,
            ml_load_model TEXT,
            status TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE foxess_history (
            timestamp TEXT,
            pv_kw REAL,
            load_kw REAL,
            grid_import_kw REAL,
            grid_export_kw REAL,
            battery_charge_kw REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE economic_plan_actions (
            plan_date TEXT,
            created_at TEXT,
            charge_kwh REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_validation VALUES (
            '2026-10-01', 20, 22, 19, 23, 18, 20,
            10, 0, NULL, NULL, NULL, 1, 12,
            0.32, 2.80, 2.48, 'load_candidate_v2', 'VALIDATED'
        )
        """
    )
    conn.execute(
        "INSERT INTO economic_plan_actions VALUES (?, ?, ?)",
        ("2026-10-01", "2026-10-01T09:45:00+10:00", 30.0),
    )
    # A later reconciliation correctly plans no remaining import, but it must
    # not replace the morning forecast in the completed-day comparison.
    conn.execute(
        "INSERT INTO economic_plan_actions VALUES (?, ?, ?)",
        ("2026-10-01", "2026-10-01T14:00:00+10:00", 0.0),
    )

    # Complete cheap window: 8 kW of the 10 kW grid import feeds the battery.
    cheap_start = datetime(2026, 10, 1, 10, 5, tzinfo=timezone(timedelta(hours=10)))
    for index in range(47):
        stamp = cheap_start + timedelta(minutes=5 * index)
        conn.execute(
            "INSERT INTO foxess_history VALUES (?, ?, ?, ?, ?, ?)",
            (stamp.isoformat(), 0.0, 2.0, 10.0, 0.0, 8.0),
        )

    # Four complete premium hours at 2 kW = 8 kWh and $2.24.
    start = datetime(2026, 10, 1, 16, 55, tzinfo=timezone(timedelta(hours=10)))
    for index in range(51):
        stamp = start + timedelta(minutes=5 * index)
        export_kw = 2.0 if 17 <= stamp.hour < 21 else 0.0
        conn.execute(
            "INSERT INTO foxess_history VALUES (?, ?, ?, ?, ?, ?)",
            (stamp.isoformat(), 0.0, 0.0, 0.0, export_kw, 0.0),
        )
    conn.commit()
    conn.close()

    result = calculate(str(db_path))
    row = result["daily"][0]

    assert result["validated_days"] == 1
    assert row["premium_export_actual_kwh"] == 8.0
    assert row["premium_revenue_actual"] == 2.24
    assert row["premium_export_error_kwh"] == 2.0
    assert result["metrics"]["solar"]["mae"] == 2.0
    assert result["metrics"]["home_use"]["mae"] == 1.0
    assert row["cheap_import_forecast_kwh"] == 30.0
    assert row["cheap_import_actual_kwh"] == 30.67
    assert row["grid_import_forecast_kwh"] == 30.0
    assert row["grid_import_actual_kwh"] == 30.67
    assert row["total_grid_import_actual_kwh"] == 1.0
    assert result["metrics"]["cheap_import"]["comparable_days"] == 1
    assert result["metrics"]["cheap_import"]["mae"] == 0.67
    assert result["metrics"]["grid_import"] == result["metrics"]["cheap_import"]
    assert result["model"]["name"] == "Extra Trees"
    assert result["model"]["trees"] == 600
    assert result["model"]["version"] == "load_candidate_v2"
    assert result["learning"]["daily_results_check"] is True
    assert result["learning"]["automatic_daily_retraining"] is False
    assert result["learning"]["holdout_days"] == 28


def test_incomplete_premium_window_is_not_treated_as_zero(tmp_path):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE strategy_validation (
            plan_date TEXT, forecast_solar_kwh REAL, forecast_load_kwh REAL,
            ml_load_forecast_kwh REAL, ml_safe_load_kwh REAL,
            actual_solar_kwh REAL, actual_load_kwh REAL,
            recommended_export_kwh REAL, predicted_grid_import_kwh REAL,
            predicted_import_cost REAL, predicted_export_revenue REAL,
            predicted_net_value REAL, actual_grid_import_kwh REAL,
            actual_grid_export_kwh REAL, actual_import_cost REAL,
            actual_export_revenue REAL, actual_net_value REAL,
            ml_load_model TEXT, status TEXT
        );
        CREATE TABLE foxess_history (
            timestamp TEXT, pv_kw REAL, load_kw REAL, grid_import_kw REAL,
            grid_export_kw REAL, battery_charge_kw REAL
        );
        CREATE TABLE economic_plan_actions (
            plan_date TEXT, created_at TEXT, charge_kwh REAL
        );
        INSERT INTO strategy_validation VALUES (
            '2026-10-01', 20, 22, 19, 23, 18, 20,
            10, NULL, NULL, NULL, NULL, 1, 12,
            0.32, 2.80, 2.48, 'load_candidate_v2', 'VALIDATED'
        );
        INSERT INTO economic_plan_actions VALUES (
            '2026-10-01', '2026-10-01T09:45:00+10:00', 30
        );
        """
    )
    conn.execute(
        "INSERT INTO foxess_history VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-10-01T10:05:00+10:00", 0.0, 2.0, 10.0, 0.0, 8.0),
    )
    conn.execute(
        "INSERT INTO foxess_history VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-10-01T10:10:00+10:00", 0.0, 2.0, 10.0, 0.0, 8.0),
    )
    conn.execute(
        "INSERT INTO foxess_history VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-10-01T17:00:00+10:00", 0.0, 0.0, 0.0, 2.0, 0.0),
    )
    conn.execute(
        "INSERT INTO foxess_history VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-10-01T17:05:00+10:00", 0.0, 0.0, 0.0, 2.0, 0.0),
    )
    conn.commit()
    conn.close()

    result = calculate(str(db_path))

    assert result["daily"][0]["premium_export_actual_kwh"] is None
    assert result["daily"][0]["cheap_import_forecast_kwh"] == 30.0
    assert result["daily"][0]["cheap_import_actual_kwh"] is None
    assert result["metrics"]["premium_export"]["comparable_days"] == 0
    assert result["metrics"]["cheap_import"]["comparable_days"] == 0

