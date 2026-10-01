import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import joblib
import pandas as pd


BASE = Path.home() / "smart-electricity"
DB = BASE / "data" / "energy.db"
MODEL_FILE = BASE / "models" / "load_candidate_v2.joblib"

TZ = ZoneInfo("Australia/Sydney")

# Temporary safety reserve while forward validation accumulates.
SAFETY_RESERVE_KWH = 4.0


def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def load_model():
    if not MODEL_FILE.exists():
        raise FileNotFoundError(
            f"Load model not found: {MODEL_FILE}"
        )

    return joblib.load(MODEL_FILE)


def historical_hourly_load(c):
    rows = c.execute("""
        SELECT
            substr(timestamp,1,13) || ':00' AS timestamp,
            AVG(load_kw) AS load_kw
        FROM foxess_history
        WHERE load_kw IS NOT NULL
        GROUP BY substr(timestamp,1,13)
        ORDER BY timestamp
    """).fetchall()

    return {
        r["timestamp"]: float(r["load_kw"] or 0)
        for r in rows
    }


def daily_history(c):
    rows = c.execute("""
        SELECT
            substr(timestamp,1,10) AS day,
            SUM(load_kw) / 12.0 AS load_kwh
        FROM foxess_history
        WHERE load_kw IS NOT NULL
        GROUP BY substr(timestamp,1,10)
        ORDER BY day
    """).fetchall()

    return {
        r["day"]: float(r["load_kwh"] or 0)
        for r in rows
    }


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


def predict_day(day=None):

    package = load_model()

    model = package["model"]
    features = package["features"]

    if day is None:
        # Shadow validation predicts the current day using
        # only information available from completed prior days.
        day = datetime.now(TZ).date()

    if isinstance(day, str):
        day = datetime.strptime(
            day,
            "%Y-%m-%d"
        ).date()

    day_string = day.isoformat()

    c = connect()

    weather = weather_rows(
        c,
        day_string
    )

    if len(weather) < 20:
        c.close()

        return {
            "available": False,
            "date": day_string,
            "reason": "Insufficient weather forecast"
        }

    hourly_history = historical_hourly_load(c)
    daily = daily_history(c)

    yesterday = day - timedelta(days=1)
    seven_days_ago = day - timedelta(days=7)

    previous_day_total = daily.get(
        yesterday.isoformat()
    )

    previous_days = []

    for i in range(1, 8):
        d = (
            day - timedelta(days=i)
        ).isoformat()

        if d in daily:
            previous_days.append(daily[d])

    rolling_7d = (
        sum(previous_days) / len(previous_days)
        if previous_days
        else None
    )

    if (
        previous_day_total is None
        or rolling_7d is None
    ):
        c.close()

        return {
            "available": False,
            "date": day_string,
            "reason": "Insufficient load history"
        }

    rows = []

    for w in weather:

        dt = datetime.fromisoformat(
            w["timestamp"]
        )

        hour = dt.hour

        yesterday_hour = (
            yesterday.isoformat()
            + f"T{hour:02d}:00"
        )

        week_hour = (
            seven_days_ago.isoformat()
            + f"T{hour:02d}:00"
        )

        same_hour_yesterday = (
            hourly_history.get(yesterday_hour)
        )

        same_hour_7d = (
            hourly_history.get(week_hour)
        )

        if (
            same_hour_yesterday is None
            or same_hour_7d is None
        ):
            continue

        # Python weekday:
        # Monday=0 ... Sunday=6
        # Training table used SQLite weekday:
        # Sunday=0 ... Saturday=6
        sqlite_dow = (
            (dt.weekday() + 1) % 7
        )

        row = {
            "hour": hour,
            "day_of_week": sqlite_dow,
            "month": dt.month,
            "is_weekend":
                1 if sqlite_dow in (0, 6) else 0,

            "temperature":
                w["temperature"],

            "apparent_temperature":
                w["apparent_temperature"],

            "cloud_cover":
                w["cloud_cover"],

            "precipitation":
                w["precipitation"],

            "load_same_hour_yesterday":
                same_hour_yesterday,

            "load_same_hour_7d":
                same_hour_7d,

            "previous_day_total_load":
                previous_day_total,

            "rolling_7d_daily_load":
                rolling_7d,
        }

        rows.append(row)

    c.close()

    if len(rows) < 20:
        return {
            "available": False,
            "date": day_string,
            "reason":
                "Insufficient hourly lag history"
        }

    X = pd.DataFrame(rows)

    prediction = model.predict(
        X[features]
    )

    prediction = [
        max(0.0, float(x))
        for x in prediction
    ]

    expected_load = sum(prediction)

    safe_load = (
        expected_load
        + SAFETY_RESERVE_KWH
    )

    return {
        "available": True,

        "date": day_string,

        "model":
            package.get(
                "version",
                "load_candidate_v2"
            ),

        "status": "SHADOW",

        "predicted_load_kwh":
            round(expected_load, 2),

        "safety_reserve_kwh":
            round(SAFETY_RESERVE_KWH, 2),

        "safe_planning_load_kwh":
            round(safe_load, 2),

        "training_daily_mae":
            round(
                package.get(
                    "ml_daily_mae",
                    0
                ),
                2
            ),

        "training_daily_bias":
            round(
                package.get(
                    "ml_daily_bias",
                    0
                ),
                2
            ),

        "high_load_risk":
            bool(
                package.get(
                    "high_load_risk",
                    False
                )
            ),

        "hours_predicted":
            len(prediction),

        "hourly_predictions": [
            {
                "hour": rows[i]["hour"],
                "load_kwh":
                    round(prediction[i], 3)
            }
            for i in range(len(prediction))
        ]
    }


if __name__ == "__main__":

    import json

    print(
        json.dumps(
            predict_day(),
            indent=2
        )
    )
