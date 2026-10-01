import sqlite3
from pathlib import Path

DB = Path.home() / "smart-electricity" / "data" / "energy.db"


def main():

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    conn.execute("""
    CREATE TABLE IF NOT EXISTS ml_training_hourly (

        timestamp TEXT PRIMARY KEY,

        hour INTEGER,
        day_of_week INTEGER,
        month INTEGER,
        is_weekend INTEGER,

        pv_kwh REAL,
        load_kwh REAL,

        grid_import_kwh REAL,
        grid_export_kwh REAL,

        battery_charge_kwh REAL,
        battery_discharge_kwh REAL,

        start_soc REAL,
        end_soc REAL,

        temperature REAL,
        apparent_temperature REAL,
        cloud_cover REAL,
        precipitation REAL,
        wind_speed REAL,
        shortwave_radiation REAL,
        direct_radiation REAL,
        diffuse_radiation REAL,
        sunshine_duration REAL,

        sample_count INTEGER
    )
    """)

    conn.execute("DELETE FROM ml_training_hourly")

    # Aggregate FoxESS 5-minute history into hourly observations.
    rows = conn.execute("""
        SELECT
            substr(timestamp,1,13) || ':00' AS hour_ts,

            CAST(substr(timestamp,12,2) AS INTEGER) AS hour,

            SUM(COALESCE(pv_kw,0)) / 12.0 AS pv_kwh,
            SUM(COALESCE(load_kw,0)) / 12.0 AS load_kwh,

            SUM(COALESCE(grid_import_kw,0)) / 12.0
                AS grid_import_kwh,

            SUM(COALESCE(grid_export_kw,0)) / 12.0
                AS grid_export_kwh,

            SUM(COALESCE(battery_charge_kw,0)) / 12.0
                AS battery_charge_kwh,

            SUM(COALESCE(battery_discharge_kw,0)) / 12.0
                AS battery_discharge_kwh,

            MIN(timestamp) AS first_ts,
            MAX(timestamp) AS last_ts,

            COUNT(*) AS sample_count

        FROM foxess_history

        GROUP BY substr(timestamp,1,13)

        ORDER BY hour_ts
    """).fetchall()

    inserted = 0

    for r in rows:

        # Require enough 5-minute samples to represent an hour.
        if r["sample_count"] < 10:
            continue

        first_soc = conn.execute("""
            SELECT battery_soc
            FROM foxess_history
            WHERE timestamp = ?
        """, (r["first_ts"],)).fetchone()

        last_soc = conn.execute("""
            SELECT battery_soc
            FROM foxess_history
            WHERE timestamp = ?
        """, (r["last_ts"],)).fetchone()

        # Weather timestamps are YYYY-MM-DDTHH:00
        weather = conn.execute("""
            SELECT *
            FROM weather
            WHERE timestamp = ?
            LIMIT 1
        """, (r["hour_ts"],)).fetchone()

        if weather is None:
            continue

        dt = r["hour_ts"]

        # SQLite weekday:
        # 0=Sunday ... 6=Saturday
        dow = int(conn.execute(
            "SELECT strftime('%w', ?)",
            (dt,)
        ).fetchone()[0])

        month = int(dt[5:7])

        conn.execute("""
        INSERT OR REPLACE INTO ml_training_hourly (

            timestamp,
            hour,
            day_of_week,
            month,
            is_weekend,

            pv_kwh,
            load_kwh,

            grid_import_kwh,
            grid_export_kwh,

            battery_charge_kwh,
            battery_discharge_kwh,

            start_soc,
            end_soc,

            temperature,
            apparent_temperature,
            cloud_cover,
            precipitation,
            wind_speed,
            shortwave_radiation,
            direct_radiation,
            diffuse_radiation,
            sunshine_duration,

            sample_count
        )

        VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """, (

            r["hour_ts"],
            r["hour"],
            dow,
            month,
            1 if dow in (0,6) else 0,

            r["pv_kwh"],
            r["load_kwh"],

            r["grid_import_kwh"],
            r["grid_export_kwh"],

            r["battery_charge_kwh"],
            r["battery_discharge_kwh"],

            first_soc["battery_soc"] if first_soc else None,
            last_soc["battery_soc"] if last_soc else None,

            weather["temperature"],
            weather["apparent_temperature"],
            weather["cloud_cover"],
            weather["precipitation"],
            weather["wind_speed"],
            weather["shortwave_radiation"],
            weather["direct_radiation"],
            weather["diffuse_radiation"],
            weather["sunshine_duration"],

            r["sample_count"]
        ))

        inserted += 1

    conn.commit()

    result = conn.execute("""
        SELECT
            MIN(timestamp),
            MAX(timestamp),
            COUNT(*)
        FROM ml_training_hourly
    """).fetchone()

    print()
    print("ML HOURLY TRAINING DATA")
    print("=======================")
    print("Rows:", result[2])
    print("From:", result[0])
    print("To:  ", result[1])
    print()

    conn.close()


if __name__ == "__main__":
    main()
