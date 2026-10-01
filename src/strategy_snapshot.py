import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from export_planner import (
    calculate,
    connect,
    load_profile,
    full_day_load
)

from load_predictor import predict_day as predict_load_day

DB = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")


def main():

    now = datetime.now(TZ)
    today = now.date().isoformat()

    # A Strategy Validation prediction must be immutable.
    # If today's snapshot already exists, do not recalculate
    # or overwrite it.
    check_conn = sqlite3.connect(DB)

    existing = check_conn.execute(
        """
        SELECT
            snapshot_timestamp,
            forecast_solar_kwh,
            forecast_load_kwh,
            ml_load_forecast_kwh,
            ml_safe_load_kwh,
            recommended_export_kwh
        FROM strategy_validation
        WHERE plan_date = ?
        """,
        (today,)
    ).fetchone()

    check_conn.close()

    if existing:
        print("STRATEGY VALIDATION SNAPSHOT")
        print("============================")
        print("Date:", today)
        print("Status: ALREADY SNAPSHOTTED")
        print("Existing snapshot:", existing[0])
        print()
        print("Solar forecast:", existing[1], "kWh")
        print("Baseline load:", existing[2], "kWh")
        print("ML v2 load:", existing[3], "kWh")
        print("Safe ML load:", existing[4], "kWh")
        print("Recommended export:", existing[5], "kWh")
        print()
        print("No changes made.")
        return

    plan = calculate()

    # Full 24-hour household consumption forecast.
    # This is used for Strategy Validation so forecast and
    # actual consumption cover the same calendar-day period.
    planner_conn = connect()
    profile, load_source = load_profile(planner_conn)
    predicted_full_day_load = full_day_load(profile)
    planner_conn.close()

    if not plan.get("available"):
        raise RuntimeError(
            "Export plan unavailable: " +
            str(plan.get("reason"))
        )

    # ML v2 load prediction is SHADOW ONLY.
    # It does not affect the export planner.
    ml_load = predict_load_day(today)

    if ml_load.get("available"):
        ml_load_forecast = ml_load.get(
            "predicted_load_kwh"
        )
        ml_safe_load = ml_load.get(
            "safe_planning_load_kwh"
        )
        ml_safety_reserve = ml_load.get(
            "safety_reserve_kwh"
        )
        ml_load_model = ml_load.get("model")
    else:
        ml_load_forecast = None
        ml_safe_load = None
        ml_safety_reserve = None
        ml_load_model = None

    today_plan = next(
        (
            x for x in plan.get("plans", [])
            if x.get("date") == today
        ),
        None
    )

    if not today_plan:
        raise RuntimeError(
            f"No export plan found for {today}"
        )

    conn = sqlite3.connect(DB)

    conn.execute(
        """
        INSERT INTO strategy_validation (
            plan_date,
            snapshot_timestamp,

            forecast_solar_kwh,
            forecast_remaining_solar_kwh,
            forecast_load_kwh,

            recommended_export_kwh,
            recommended_export_percent,

            predicted_start_soc,
            predicted_pre_export_soc,
            predicted_midnight_soc,
            predicted_next_solar_soc,

            foxess_control_mode,
            foxess_export_start,
            foxess_export_end,
            foxess_cutoff_soc,

            ml_load_forecast_kwh,
            ml_safe_load_kwh,
            ml_load_safety_reserve_kwh,
            ml_load_model,

            status
        )
        VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )

        ON CONFLICT(plan_date) DO NOTHING
        """,
        (
            today,
            now.isoformat(),

            today_plan.get("full_day_solar_kwh"),
            today_plan.get("remaining_solar_kwh"),
            round(predicted_full_day_load, 2),

            today_plan.get("recommended_export_kwh"),
            today_plan.get("recommended_export_percent"),

            today_plan.get("starting_soc"),
            today_plan.get("pre_export_soc"),
            today_plan.get("midnight_soc"),
            today_plan.get("next_solar_start_soc"),

            "FIXED_SCHEDULE",
            "17:00",
            "21:00",
            60.0,

            ml_load_forecast,
            ml_safe_load,
            ml_safety_reserve,
            ml_load_model,

            "PENDING"
        )
    )

    conn.commit()
    conn.close()

    print("STRATEGY VALIDATION SNAPSHOT")
    print("============================")
    print("Date:", today)
    print("Time:", now.isoformat())
    print()
    print(
        "Solar forecast:",
        today_plan.get("full_day_solar_kwh"),
        "kWh"
    )
    print(
        "Load forecast:",
        round(predicted_full_day_load, 2),
        "kWh"
    )
    print(
        "Baseline load forecast:",
        round(predicted_full_day_load, 2),
        "kWh"
    )

    if ml_load_forecast is not None:
        print(
            "ML v2 load forecast:",
            ml_load_forecast,
            "kWh"
        )
        print(
            "ML safe planning load:",
            ml_safe_load,
            "kWh"
        )
        print(
            "ML safety reserve:",
            ml_safety_reserve,
            "kWh"
        )
        print(
            "ML load model:",
            ml_load_model
        )
    else:
        print(
            "ML load forecast unavailable:",
            ml_load.get("reason")
        )

    print(
        "ML export:",
        today_plan.get("recommended_export_percent"),
        "% /",
        today_plan.get("recommended_export_kwh"),
        "kWh"
    )
    print(
        "Before 5PM SOC:",
        today_plan.get("pre_export_soc"),
        "%"
    )
    print(
        "Predicted next-solar SOC:",
        today_plan.get("next_solar_start_soc"),
        "%"
    )
    print()
    print("FoxESS actual strategy:")
    print("17:00-21:00 / cutoff 60%")
    print()
    print("CONTROL MODE: SHADOW")
    print("No FoxESS settings changed.")


if __name__ == "__main__":
    main()
