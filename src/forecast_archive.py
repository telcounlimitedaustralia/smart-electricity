"""Archive immutable D+1..D+7 forecasts and score completed targets."""

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from forward_load_predictor import forecast_days
from export_planner import connect, forecast, pv_for_hour

DB = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forecast_vintages (
            forecast_created TEXT NOT NULL,
            target_date TEXT NOT NULL,
            horizon_day INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            point_load_kwh REAL NOT NULL,
            safe_load_kwh REAL NOT NULL,
            buffer_kwh REAL NOT NULL,
            solar_kwh REAL,
            hourly_json TEXT NOT NULL,
            actual_load_kwh REAL,
            actual_solar_kwh REAL,
            point_error_kwh REAL,
            solar_error_kwh REAL,
            safe_shortfall_kwh REAL,
            scored_at TEXT,
            PRIMARY KEY (forecast_created, target_date, model_version)
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(forecast_vintages)")}
    if "solar_kwh" not in columns:
        conn.execute("ALTER TABLE forecast_vintages ADD COLUMN solar_kwh REAL")
    if "actual_solar_kwh" not in columns:
        conn.execute("ALTER TABLE forecast_vintages ADD COLUMN actual_solar_kwh REAL")
    if "solar_error_kwh" not in columns:
        conn.execute("ALTER TABLE forecast_vintages ADD COLUMN solar_error_kwh REAL")


def archive(conn, now):
    # Archive the same today-through-D+6 horizon consumed by the optimiser.
    # Today's predecessor is complete yesterday; later days are recursive.
    result = forecast_days(start_day=now.date(), days=7)
    weather_conn = connect()
    solar_by_day = {}
    for weather in forecast(weather_conn):
        day = weather["timestamp"][:10]
        solar_by_day[day] = solar_by_day.get(day, 0.0) + float(
            pv_for_hour(weather["shortwave_radiation"])
        )
    weather_conn.close()
    created = now.isoformat(timespec="seconds")
    inserted = 0
    for row in result.get("forecast_days", []):
        if not row.get("available"):
            continue
        conn.execute("""
            INSERT OR IGNORE INTO forecast_vintages (
                forecast_created, target_date, horizon_day, model_version,
                point_load_kwh, safe_load_kwh, buffer_kwh, solar_kwh, hourly_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            created, row["date"], row["horizon_day"], row.get("model", "unknown"),
            row["predicted_load_kwh"], row["safe_load_kwh"],
            row["planning_buffer_kwh"], solar_by_day.get(row["date"]),
            json.dumps(row["hourly_predictions"]),
        ))
        inserted += 1
    return inserted


def score(conn, now):
    pending = conn.execute("""
        SELECT DISTINCT target_date FROM forecast_vintages
        WHERE actual_load_kwh IS NULL AND target_date < ?
    """, (now.date().isoformat(),)).fetchall()
    scored = 0
    for (day,) in pending:
        actual = conn.execute("""
            SELECT SUM(load_kw) / 12.0 FROM foxess_history
            WHERE substr(timestamp, 1, 10) = ?
        """, (day,)).fetchone()[0]
        samples = conn.execute("""
            SELECT COUNT(*) FROM foxess_history
            WHERE substr(timestamp, 1, 10) = ?
        """, (day,)).fetchone()[0]
        if actual is None or samples < 276:
            continue
        actual_solar = conn.execute("""
            SELECT SUM(pv_kw) / 12.0 FROM foxess_history
            WHERE substr(timestamp, 1, 10) = ?
        """, (day,)).fetchone()[0]
        conn.execute("""
            UPDATE forecast_vintages SET
                actual_load_kwh = ?,
                actual_solar_kwh = ?,
                point_error_kwh = point_load_kwh - ?,
                solar_error_kwh = solar_kwh - ?,
                safe_shortfall_kwh = MAX(0, ? - safe_load_kwh),
                scored_at = ?
            WHERE target_date = ? AND actual_load_kwh IS NULL
        """, (
            actual, actual_solar, actual, actual_solar, actual,
            now.isoformat(timespec="seconds"), day,
        ))
        scored += 1
    return scored


def main():
    now = datetime.now(TZ)
    conn = sqlite3.connect(DB)
    ensure_schema(conn)
    scored = score(conn, now)
    inserted = archive(conn, now)
    conn.commit()
    conn.close()
    print(f"forecast archive: inserted={inserted}, completed_days_scored={scored}")


if __name__ == "__main__":
    main()
