"""
Smart Electricity
Recursive Forward Load Predictor v1

Purpose:
- Produce multi-day hourly household-load forecasts.
- Reuse the existing load_candidate_v2 ExtraTrees model.
- Use actual history/live data whenever available.
- Recursively use prior predicted days when forecasting farther ahead.

SHADOW ONLY.
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import joblib
import pandas as pd


BASE = Path.home() / "smart-electricity"
DB = BASE / "data" / "energy.db"
MODEL_FILE = BASE / "models" / "load_candidate_v2.joblib"

TZ = ZoneInfo("Australia/Sydney")
FIVE_MINUTE_SECONDS = 5 * 60
COMPLETED_DAY_MISSING_TOLERANCE = 12


def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def load_model():
    return joblib.load(MODEL_FILE)


def actual_hourly_load(c):
    """
    Build hourly load dictionary.

    Completed history is preferred.
    foxess_live fills current-day gaps.
    """

    values = {}

    history = c.execute("""
        SELECT
            substr(timestamp,1,13) || ':00' AS ts,
            AVG(load_kw) AS load_kw
        FROM foxess_history
        WHERE load_kw IS NOT NULL
        GROUP BY substr(timestamp,1,13)
        ORDER BY ts
    """).fetchall()

    for r in history:
        values[r["ts"]] = float(r["load_kw"] or 0)

    live = c.execute("""
        SELECT
            substr(timestamp,1,13) || ':00' AS ts,
            AVG(load_kw) AS load_kw
        FROM foxess_live
        WHERE load_kw IS NOT NULL
        GROUP BY substr(timestamp,1,13)
        ORDER BY ts
    """).fetchall()

    for r in live:
        if r["ts"] not in values:
            values[r["ts"]] = float(r["load_kw"] or 0)

    return values


def expected_five_minute_readings(day):
    """Return the physical sample count for a Sydney calendar day."""
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    elapsed = (
        end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    ).total_seconds()
    return int(elapsed / FIVE_MINUTE_SECONDS)


def actual_daily_load(c, today=None):
    """
    Build daily load totals.

    Prefer foxess_history.

    If a completed prior day has not yet been backfilled into
    foxess_history, use foxess_live when it contains a sufficiently
    complete day.

    Incomplete current-day data is never treated as a completed day.
    Nothing is written to the database.
    """

    values = {}

    # Proper historical records first.
    rows = c.execute("""
        SELECT
            substr(timestamp,1,10) AS day,
            SUM(load_kw) / 12.0 AS load_kwh
        FROM foxess_history
        WHERE load_kw IS NOT NULL
        GROUP BY substr(timestamp,1,10)
        ORDER BY day
    """).fetchall()

    for r in rows:
        values[r["day"]] = float(
            r["load_kwh"] or 0
        )

    today = today or datetime.now(TZ).date()

    # Find live days not already represented by history.
    live_days = c.execute("""
        SELECT
            substr(timestamp,1,10) AS day,
            COUNT(*) AS readings,
            MAX(timestamp) AS last_timestamp,
            SUM(load_kw) / 12.0 AS load_kwh
        FROM foxess_live
        WHERE load_kw IS NOT NULL
        GROUP BY substr(timestamp,1,10)
        ORDER BY day
    """).fetchall()

    for r in live_days:

        day_string = r["day"]

        if day_string in values:
            continue

        try:
            day_date = datetime.strptime(
                day_string,
                "%Y-%m-%d"
            ).date()
        except ValueError:
            continue

        # Never treat today's partial data as a completed day.
        if day_date >= today:
            continue

        readings = int(
            r["readings"] or 0
        )

        last_timestamp = r[
            "last_timestamp"
        ]

        if not last_timestamp:
            continue

        last_hour = datetime.fromisoformat(
            last_timestamp
        ).hour

        # Require all but at most one hour of the physical day, plus data
        # extending into its final wall-clock hour.  Sydney days can contain
        # 23, 24 or 25 hours across daylight-saving transitions.
        minimum_readings = max(
            1,
            expected_five_minute_readings(day_date)
            - COMPLETED_DAY_MISSING_TOLERANCE,
        )
        if (
            readings >= minimum_readings
            and last_hour >= 23
        ):
            values[day_string] = float(
                r["load_kwh"] or 0
            )

    return values


def weather_rows(c, day):
    rows = c.execute("""
        SELECT
            timestamp,
            temperature,
            apparent_temperature,
            cloud_cover,
            precipitation
        FROM weather
        WHERE substr(timestamp,1,10) = ?
        ORDER BY timestamp
    """, (day,)).fetchall()

    return [dict(r) for r in rows]


def hourly_key(day, hour):
    return (
        day.isoformat()
        + f"T{hour:02d}:00"
    )


def rolling_daily_average(day, daily_values):
    vals = []

    for i in range(1, 8):
        d = (
            day - timedelta(days=i)
        ).isoformat()

        if d in daily_values:
            vals.append(
                float(daily_values[d])
            )

    if not vals:
        return None

    return sum(vals) / len(vals)


def forecast_days(start_day=None, days=7):

    package = load_model()
    model = package["model"]
    features = package["features"]

    if start_day is None:
        start_day = (
            datetime.now(TZ).date()
            + timedelta(days=1)
        )

    if isinstance(start_day, str):
        start_day = datetime.strptime(
            start_day,
            "%Y-%m-%d"
        ).date()

    c = connect()

    # These dictionaries evolve as predictions are made.
    hourly_values = actual_hourly_load(c)
    daily_values = actual_daily_load(c)

    results = []

    for horizon in range(days):

        day = start_day + timedelta(
            days=horizon
        )

        day_string = day.isoformat()

        weather = weather_rows(
            c,
            day_string
        )

        if len(weather) < 20:
            results.append({
                "available": False,
                "date": day_string,
                "horizon_day": horizon + 1,
                "reason":
                    "Insufficient weather forecast"
            })
            break

        yesterday = day - timedelta(days=1)
        seven_days_ago = (
            day - timedelta(days=7)
        )

        previous_day_total = daily_values.get(
            yesterday.isoformat()
        )

        rolling_7d = rolling_daily_average(
            day,
            daily_values
        )

        if previous_day_total is None:
            results.append({
                "available": False,
                "date": day_string,
                "horizon_day": horizon + 1,
                "reason":
                    "Previous-day total unavailable"
            })
            break

        if rolling_7d is None:
            results.append({
                "available": False,
                "date": day_string,
                "horizon_day": horizon + 1,
                "reason":
                    "Rolling 7-day load unavailable"
            })
            break

        rows = []

        for w in weather:

            dt = datetime.fromisoformat(
                w["timestamp"]
            )

            hour = dt.hour

            lag_1 = hourly_values.get(
                hourly_key(
                    yesterday,
                    hour
                )
            )

            lag_7 = hourly_values.get(
                hourly_key(
                    seven_days_ago,
                    hour
                )
            )

            if lag_1 is None or lag_7 is None:
                continue

            sqlite_dow = (
                (dt.weekday() + 1) % 7
            )

            rows.append({
                "hour": hour,
                "day_of_week": sqlite_dow,
                "month": dt.month,
                "is_weekend":
                    1
                    if sqlite_dow in (0, 6)
                    else 0,

                "temperature":
                    w["temperature"],

                "apparent_temperature":
                    w["apparent_temperature"],

                "cloud_cover":
                    w["cloud_cover"],

                "precipitation":
                    w["precipitation"],

                "load_same_hour_yesterday":
                    lag_1,

                "load_same_hour_7d":
                    lag_7,

                "previous_day_total_load":
                    previous_day_total,

                "rolling_7d_daily_load":
                    rolling_7d,
            })

        if len(rows) < 20:
            results.append({
                "available": False,
                "date": day_string,
                "horizon_day": horizon + 1,
                "reason":
                    "Insufficient hourly lag history"
            })
            break

        X = pd.DataFrame(rows)

        prediction = model.predict(
            X[features]
        )

        prediction = [
            max(0.0, float(x))
            for x in prediction
        ]

        daily_total = sum(prediction)

        hourly_predictions = []

        for i, value in enumerate(prediction):

            hour = rows[i]["hour"]

            hourly_values[
                hourly_key(day, hour)
            ] = value

            hourly_predictions.append({
                "hour": hour,
                "load_kwh":
                    round(value, 3)
            })

        # Critical recursive step:
        # tomorrow can now use today's predicted
        # total as previous_day_total_load.
        daily_values[day_string] = daily_total

        # Uncertainty metadata.
        #
        # We are NOT pretending this is a statistical
        # confidence interval yet. It is a risk indicator
        # derived from model validation performance.
        base_mae = float(
            package.get(
                "ml_daily_mae",
                0.0
            )
        )

        calibrated_p90 = float(
            package.get(
                "underprediction_buffer_p90",
                base_mae
            )
        )

        worst_under = abs(
            float(
                package.get(
                    "worst_daily_underprediction",
                    0.0
                )
            )
        )

        # Recursive uncertainty grows with horizon.
        uncertainty_multiplier = (
            1.0 + 0.25 * horizon
        )

        planning_buffer = min(
            worst_under,
            max(base_mae, calibrated_p90)
            * uncertainty_multiplier
        )

        safe_daily_load = (
            daily_total
            + planning_buffer
        )

        results.append({
            "available": True,
            "date": day_string,
            "horizon_day": horizon + 1,

            "model":
                package.get(
                    "version",
                    "load_candidate_v2"
                ),

            "recursive":
                horizon > 0,

            "predicted_load_kwh":
                round(daily_total, 2),

            "planning_buffer_kwh":
                round(planning_buffer, 2),

            "safe_load_kwh":
                round(safe_daily_load, 2),

            "training_mae_kwh":
                round(base_mae, 2),

            "calibrated_underprediction_p90_kwh":
                round(calibrated_p90, 2),

            "worst_training_underprediction_kwh":
                round(worst_under, 2),

            "hours_predicted":
                len(prediction),

            "hourly_predictions":
                hourly_predictions,
        })

    c.close()

    return {
        "available":
            bool(results)
            and results[0].get(
                "available",
                False
            ),

        "model":
            package.get(
                "version",
                "load_candidate_v2"
            ),

        "architecture":
            "recursive_forward_load_v1",

        "requested_days":
            days,

        "forecast_days":
            results,
    }


def print_report(result):

    print(
        "SMART ELECTRICITY - "
        "FORWARD LOAD ML"
    )
    print("=" * 50)

    print(
        "Model:",
        result.get("model")
    )

    print(
        "Architecture:",
        result.get("architecture")
    )

    print()

    for d in result.get(
        "forecast_days",
        []
    ):

        print(
            d["date"],
            f'(D+{d["horizon_day"]})'
        )

        if not d.get("available"):
            print(
                "  UNAVAILABLE:",
                d.get("reason")
            )
            print()
            continue

        print(
            "  Predicted:",
            d["predicted_load_kwh"],
            "kWh"
        )

        print(
            "  Planning buffer:",
            d["planning_buffer_kwh"],
            "kWh"
        )

        print(
            "  Safe load:",
            d["safe_load_kwh"],
            "kWh"
        )

        print(
            "  Recursive:",
            d["recursive"]
        )

        print(
            "  Hours:",
            d["hours_predicted"]
        )

        print()


if __name__ == "__main__":
    result = forecast_days(
        days=7
    )

    print_report(result)
