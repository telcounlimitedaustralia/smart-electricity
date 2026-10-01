import sqlite3
import json

DB = "data/energy.db"


def calculate():

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            plan_date,
            forecast_load_kwh,
            ml_load_forecast_kwh,
            ml_safe_load_kwh,
            actual_load_kwh,
            ml_load_model,
            status
        FROM strategy_validation
        WHERE status = 'VALIDATED'
          AND actual_load_kwh IS NOT NULL
          AND ml_load_forecast_kwh IS NOT NULL
        ORDER BY plan_date
    """).fetchall()

    conn.close()

    if not rows:
        return {
            "available": False,
            "validated_days": 0,
            "reason": "No validated ML prediction days yet"
        }

    daily = []

    for r in rows:

        actual = float(r["actual_load_kwh"])
        baseline = float(r["forecast_load_kwh"])
        ml = float(r["ml_load_forecast_kwh"])

        safe = (
            float(r["ml_safe_load_kwh"])
            if r["ml_safe_load_kwh"] is not None
            else None
        )

        baseline_error = baseline - actual
        ml_error = ml - actual

        safe_error = (
            safe - actual
            if safe is not None
            else None
        )

        if abs(ml_error) < abs(baseline_error):
            winner = "ML"
        elif abs(ml_error) > abs(baseline_error):
            winner = "BASELINE"
        else:
            winner = "TIE"

        daily.append({
            "date": r["plan_date"],
            "model": r["ml_load_model"],
            "actual_kwh": round(actual, 2),
            "baseline_kwh": round(baseline, 2),
            "ml_kwh": round(ml, 2),
            "safe_ml_kwh": (
                round(safe, 2)
                if safe is not None
                else None
            ),
            "baseline_error_kwh":
                round(baseline_error, 2),
            "ml_error_kwh":
                round(ml_error, 2),
            "safe_ml_error_kwh": (
                round(safe_error, 2)
                if safe_error is not None
                else None
            ),
            "winner": winner
        })

    baseline_errors = [
        x["baseline_error_kwh"] for x in daily
    ]

    ml_errors = [
        x["ml_error_kwh"] for x in daily
    ]

    safe_errors = [
        x["safe_ml_error_kwh"]
        for x in daily
        if x["safe_ml_error_kwh"] is not None
    ]

    baseline_mae = (
        sum(abs(x) for x in baseline_errors)
        / len(baseline_errors)
    )

    ml_mae = (
        sum(abs(x) for x in ml_errors)
        / len(ml_errors)
    )

    safe_mae = (
        sum(abs(x) for x in safe_errors)
        / len(safe_errors)
        if safe_errors
        else None
    )

    ml_bias = sum(ml_errors) / len(ml_errors)

    ml_wins = sum(
        x["winner"] == "ML"
        for x in daily
    )

    baseline_wins = sum(
        x["winner"] == "BASELINE"
        for x in daily
    )

    underpredictions = sum(
        x < 0 for x in ml_errors
    )

    worst_underprediction = min(ml_errors)

    improvement = (
        (baseline_mae - ml_mae)
        / baseline_mae * 100
        if baseline_mae > 0
        else 0
    )

    return {
        "available": True,
        "validated_days": len(daily),

        "baseline_mae_kwh":
            round(baseline_mae, 2),

        "ml_mae_kwh":
            round(ml_mae, 2),

        "safe_ml_mae_kwh": (
            round(safe_mae, 2)
            if safe_mae is not None
            else None
        ),

        "ml_bias_kwh":
            round(ml_bias, 2),

        "ml_improvement_percent":
            round(improvement, 1),

        "ml_wins": ml_wins,
        "baseline_wins": baseline_wins,

        "ml_underprediction_days":
            underpredictions,

        "worst_ml_underprediction_kwh":
            round(worst_underprediction, 2),

        "daily": daily
    }


if __name__ == "__main__":
    print(
        json.dumps(
            calculate(),
            indent=2
        )
    )
