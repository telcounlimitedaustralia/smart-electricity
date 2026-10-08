"""Forecast-versus-actual performance audit for the read-only dashboard."""

import json
import sqlite3
from datetime import datetime, time


DB = "data/energy.db"
PREMIUM_START = time(17, 0)
PREMIUM_END = time(21, 0)
PREMIUM_EXPORT_DOLLARS_PER_KWH = 0.28
MAX_INTERVAL_HOURS = 10 / 60


def _float(value):
    return None if value is None else float(value)


def _round(value, digits=2):
    return None if value is None else round(float(value), digits)


def _premium_actuals(conn, day):
    """Return actual 5-9 PM export and revenue at matching grain."""
    rows = conn.execute(
        """
        SELECT timestamp, grid_export_kw
        FROM foxess_history
        WHERE substr(timestamp, 1, 10) = ?
        ORDER BY timestamp
        """,
        (day,),
    ).fetchall()

    export_kwh = 0.0
    covered_hours = 0.0

    for first, second in zip(rows, rows[1:]):
        first_ts = datetime.fromisoformat(first["timestamp"])
        second_ts = datetime.fromisoformat(second["timestamp"])
        interval_hours = (second_ts - first_ts).total_seconds() / 3600

        if not (0 < interval_hours <= MAX_INTERVAL_HOURS):
            continue
        if not (PREMIUM_START <= first_ts.time() < PREMIUM_END):
            continue

        export_kwh += max(0.0, float(first["grid_export_kw"] or 0)) * interval_hours
        covered_hours += interval_hours

    # Require at least 90% of the four-hour comparison window.
    if covered_hours < 3.6:
        return None, None, round(covered_hours, 2)

    revenue = export_kwh * PREMIUM_EXPORT_DOLLARS_PER_KWH
    return round(export_kwh, 2), round(revenue, 2), round(covered_hours, 2)


def _metric(pairs, unit):
    comparable = [
        (float(forecast), float(actual))
        for forecast, actual in pairs
        if forecast is not None and actual is not None
    ]
    if not comparable:
        return {
            "unit": unit,
            "comparable_days": 0,
            "mae": None,
            "bias": None,
            "mape_percent": None,
        }

    errors = [forecast - actual for forecast, actual in comparable]
    percentage_errors = [
        abs(forecast - actual) / abs(actual) * 100
        for forecast, actual in comparable
        if actual != 0
    ]
    return {
        "unit": unit,
        "comparable_days": len(comparable),
        "mae": round(sum(abs(error) for error in errors) / len(errors), 2),
        "bias": round(sum(errors) / len(errors), 2),
        "mape_percent": (
            round(sum(percentage_errors) / len(percentage_errors), 1)
            if percentage_errors
            else None
        ),
    }


def calculate(db_path=None):
    conn = sqlite3.connect(db_path or DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT
            plan_date,
            forecast_solar_kwh,
            forecast_load_kwh,
            ml_load_forecast_kwh,
            ml_safe_load_kwh,
            actual_solar_kwh,
            actual_load_kwh,
            recommended_export_kwh,
            predicted_grid_import_kwh,
            predicted_import_cost,
            predicted_export_revenue,
            predicted_net_value,
            actual_grid_import_kwh,
            actual_grid_export_kwh,
            actual_import_cost,
            actual_export_revenue,
            actual_net_value,
            ml_load_model
        FROM strategy_validation
        WHERE status = 'VALIDATED'
        ORDER BY plan_date
        """
    ).fetchall()

    if not rows:
        conn.close()
        return {
            "available": False,
            "validated_days": 0,
            "reason": "No completed forecast days are available yet",
        }

    daily = []
    for row in rows:
        premium_export, premium_revenue, premium_coverage = _premium_actuals(
            conn, row["plan_date"]
        )

        solar_forecast = _float(row["forecast_solar_kwh"])
        solar_actual = _float(row["actual_solar_kwh"])
        load_forecast = _float(row["ml_load_forecast_kwh"])
        load_actual = _float(row["actual_load_kwh"])
        premium_export_forecast = _float(row["recommended_export_kwh"])
        import_forecast = _float(row["predicted_grid_import_kwh"])

        revenue_forecast = _float(row["predicted_export_revenue"])
        if revenue_forecast is None and premium_export_forecast is not None:
            revenue_forecast = (
                premium_export_forecast * PREMIUM_EXPORT_DOLLARS_PER_KWH
            )

        daily.append(
            {
                "date": row["plan_date"],
                "model": row["ml_load_model"],
                "solar_forecast_kwh": _round(solar_forecast),
                "solar_actual_kwh": _round(solar_actual),
                "solar_error_kwh": _round(
                    None
                    if solar_forecast is None or solar_actual is None
                    else solar_forecast - solar_actual
                ),
                "load_forecast_kwh": _round(load_forecast),
                "protected_load_kwh": _round(row["ml_safe_load_kwh"]),
                "load_actual_kwh": _round(load_actual),
                "load_error_kwh": _round(
                    None
                    if load_forecast is None or load_actual is None
                    else load_forecast - load_actual
                ),
                "premium_export_forecast_kwh": _round(premium_export_forecast),
                "premium_export_actual_kwh": _round(premium_export),
                "premium_export_error_kwh": _round(
                    None
                    if premium_export_forecast is None or premium_export is None
                    else premium_export_forecast - premium_export
                ),
                "total_export_actual_kwh": _round(row["actual_grid_export_kwh"]),
                "grid_import_forecast_kwh": _round(import_forecast),
                "grid_import_actual_kwh": _round(row["actual_grid_import_kwh"]),
                "export_revenue_forecast": _round(revenue_forecast),
                "premium_revenue_actual": _round(premium_revenue),
                "total_export_revenue_actual": _round(row["actual_export_revenue"]),
                "import_cost_forecast": _round(row["predicted_import_cost"]),
                "import_cost_actual": _round(row["actual_import_cost"]),
                "net_value_forecast": _round(row["predicted_net_value"]),
                "net_value_actual": _round(row["actual_net_value"]),
                "premium_window_coverage_hours": premium_coverage,
            }
        )

    conn.close()

    metrics = {
        "solar": _metric(
            [(row["solar_forecast_kwh"], row["solar_actual_kwh"]) for row in daily],
            "kWh",
        ),
        "home_use": _metric(
            [(row["load_forecast_kwh"], row["load_actual_kwh"]) for row in daily],
            "kWh",
        ),
        "premium_export": _metric(
            [
                (
                    row["premium_export_forecast_kwh"],
                    row["premium_export_actual_kwh"],
                )
                for row in daily
            ],
            "kWh",
        ),
        "grid_import": _metric(
            [
                (row["grid_import_forecast_kwh"], row["grid_import_actual_kwh"])
                for row in daily
            ],
            "kWh",
        ),
        "premium_revenue": _metric(
            [
                (row["export_revenue_forecast"], row["premium_revenue_actual"])
                for row in daily
            ],
            "$",
        ),
        "import_cost": _metric(
            [(row["import_cost_forecast"], row["import_cost_actual"]) for row in daily],
            "$",
        ),
        "net_value": _metric(
            [(row["net_value_forecast"], row["net_value_actual"]) for row in daily],
            "$",
        ),
    }

    # Retain the original load-monitor fields for existing API consumers.
    ml_metric = metrics["home_use"]
    load_days = [row for row in daily if row["load_error_kwh"] is not None]
    baseline_pairs = [
        (row["forecast_load_kwh"], row["actual_load_kwh"])
        for row in rows
        if row["forecast_load_kwh"] is not None and row["actual_load_kwh"] is not None
    ]
    safe_pairs = [
        (row["protected_load_kwh"], row["load_actual_kwh"])
        for row in daily
        if row["protected_load_kwh"] is not None and row["load_actual_kwh"] is not None
    ]
    baseline_metric = _metric(baseline_pairs, "kWh")
    safe_metric = _metric(safe_pairs, "kWh")

    ml_wins = 0
    baseline_wins = 0
    for audit_row, source_row in zip(daily, rows):
        if (
            audit_row["load_error_kwh"] is None
            or source_row["forecast_load_kwh"] is None
            or source_row["actual_load_kwh"] is None
        ):
            continue
        baseline_error = float(source_row["forecast_load_kwh"]) - float(
            source_row["actual_load_kwh"]
        )
        if abs(audit_row["load_error_kwh"]) < abs(baseline_error):
            ml_wins += 1
        elif abs(audit_row["load_error_kwh"]) > abs(baseline_error):
            baseline_wins += 1

    return {
        "available": True,
        "validated_days": len(daily),
        "evidence_label": (
            "Early evidence"
            if len(daily) < 7
            else "Growing evidence"
            if len(daily) < 30
            else "Established history"
        ),
        "metrics": metrics,
        "daily": daily,
        "model": {
            "name": "Extra Trees",
            "version": next(
                (row["model"] for row in reversed(daily) if row["model"]),
                "load_candidate_v2",
            ),
            "trees": 600,
            "inputs": [
                "time and day",
                "temperature, cloud and rain",
                "yesterday at the same hour",
                "the same hour last week",
                "recent daily home use",
            ],
        },
        "learning": {
            "daily_results_check": True,
            "automatic_daily_retraining": False,
            "holdout_days": 28,
            "promotion_rule": (
                "A refreshed model replaces the current one only when it is "
                "more accurate on the latest 28-day test and passes the "
                "underprediction safety checks."
            ),
        },
        "comparison_definition": (
            "Forecast error is forecast minus actual. Premium export and revenue "
            "compare only the same 5 PM-9 PM window; all-day actual totals are "
            "shown separately. Missing forecasts remain unavailable, never zero."
        ),
        "data_quality": {
            "validated_days": len(daily),
            "minimum_days_for_trend": 7,
            "premium_window_required_hours": 3.6,
            "import_forecast_note": (
                "Historic import forecasts were not stored. Coverage will build "
                "from new daily snapshots."
            ),
        },
        "baseline_mae_kwh": baseline_metric["mae"],
        "ml_mae_kwh": ml_metric["mae"],
        "safe_ml_mae_kwh": safe_metric["mae"],
        "ml_bias_kwh": ml_metric["bias"],
        "ml_improvement_percent": (
            round(
                (baseline_metric["mae"] - ml_metric["mae"])
                / baseline_metric["mae"]
                * 100,
                1,
            )
            if baseline_metric["mae"] not in (None, 0) and ml_metric["mae"] is not None
            else None
        ),
        "ml_wins": ml_wins,
        "baseline_wins": baseline_wins,
        "ml_underprediction_days": sum(
            row["load_error_kwh"] < 0 for row in load_days
        ),
        "worst_ml_underprediction_kwh": (
            min(row["load_error_kwh"] for row in load_days) if load_days else None
        ),
    }


if __name__ == "__main__":
    print(json.dumps(calculate(), indent=2))
