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
            ml_load_safety_reserve_kwh,
            actual_load_kwh,
            ml_load_model
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
            "reason":
                "No validated ML prediction days yet"
        }

    daily = []

    ml_under_days = 0
    safe_under_days = 0
    reserve_success_days = 0

    total_ml_shortfall = 0.0
    total_safe_shortfall = 0.0

    worst_ml_shortfall = 0.0
    worst_safe_shortfall = 0.0

    for r in rows:

        actual = float(r["actual_load_kwh"])
        ml = float(r["ml_load_forecast_kwh"])

        safe = (
            float(r["ml_safe_load_kwh"])
            if r["ml_safe_load_kwh"] is not None
            else ml
        )

        reserve = (
            float(r["ml_load_safety_reserve_kwh"])
            if r["ml_load_safety_reserve_kwh"] is not None
            else safe - ml
        )

        ml_shortfall = max(0.0, actual - ml)
        safe_shortfall = max(0.0, actual - safe)

        ml_under = ml_shortfall > 0
        safe_under = safe_shortfall > 0

        if ml_under:
            ml_under_days += 1
            total_ml_shortfall += ml_shortfall

            if not safe_under:
                reserve_success_days += 1

        if safe_under:
            safe_under_days += 1
            total_safe_shortfall += safe_shortfall

        worst_ml_shortfall = max(
            worst_ml_shortfall,
            ml_shortfall
        )

        worst_safe_shortfall = max(
            worst_safe_shortfall,
            safe_shortfall
        )

        daily.append({
            "date": r["plan_date"],
            "actual_kwh": round(actual, 2),
            "ml_kwh": round(ml, 2),
            "safe_ml_kwh": round(safe, 2),
            "reserve_kwh": round(reserve, 2),

            "ml_shortfall_kwh":
                round(ml_shortfall, 2),

            "safe_shortfall_kwh":
                round(safe_shortfall, 2),

            "reserve_covered_error":
                bool(ml_under and not safe_under)
        })

    days = len(daily)

    ml_under_rate = ml_under_days / days * 100
    safe_under_rate = safe_under_days / days * 100

    reserve_success_rate = (
        reserve_success_days / ml_under_days * 100
        if ml_under_days
        else 100.0
    )

    avg_ml_shortfall = (
        total_ml_shortfall / ml_under_days
        if ml_under_days
        else 0
    )

    avg_safe_shortfall = (
        total_safe_shortfall / safe_under_days
        if safe_under_days
        else 0
    )

    # Do not make a model-safety judgement from
    # only a handful of genuine shadow days.
    if days < 7:
        risk = "COLLECTING_DATA"

    elif safe_under_rate <= 10:
        risk = "LOW"

    elif safe_under_rate <= 25:
        risk = "MEDIUM"

    else:
        risk = "HIGH"

    return {
        "available": True,
        "validated_days": days,

        "ml_underprediction_days":
            ml_under_days,

        "ml_underprediction_rate_percent":
            round(ml_under_rate, 1),

        "safe_ml_underprediction_days":
            safe_under_days,

        "safe_ml_underprediction_rate_percent":
            round(safe_under_rate, 1),

        "reserve_success_days":
            reserve_success_days,

        "reserve_success_rate_percent":
            round(reserve_success_rate, 1),

        "average_ml_shortfall_kwh":
            round(avg_ml_shortfall, 2),

        "worst_ml_shortfall_kwh":
            round(worst_ml_shortfall, 2),

        "average_safe_shortfall_kwh":
            round(avg_safe_shortfall, 2),

        "worst_safe_shortfall_kwh":
            round(worst_safe_shortfall, 2),

        "risk_status": risk,

        "daily": daily
    }


if __name__ == "__main__":
    print(
        json.dumps(
            calculate(),
            indent=2
        )
    )
