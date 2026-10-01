import sqlite3
from datetime import datetime
from tariff import rates_at

DB = "data/energy.db"


def parse_timestamp(value):
    if value is None:
        return None

    value = value.replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def create_table(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS ml_features (
        foxess_id INTEGER PRIMARY KEY,
        timestamp TEXT NOT NULL,

        hour INTEGER,
        minute INTEGER,
        day_of_week INTEGER,
        month INTEGER,
        is_weekend INTEGER,

        pv_kw REAL,
        load_kw REAL,
        grid_import_kw REAL,
        grid_export_kw REAL,
        battery_soc REAL,
        battery_charge_kw REAL,
        battery_discharge_kw REAL,

        temperature REAL,
        cloud_cover REAL,
        precipitation REAL,
        shortwave_radiation REAL,
        sunshine_duration REAL,

        import_rate REAL,
        export_rate REAL,
        import_period TEXT,
        export_period TEXT,

        net_load_kw REAL,
        solar_surplus_kw REAL
    )
    """)


def nearest_weather(conn, timestamp):
    return conn.execute("""
        SELECT
            temperature,
            cloud_cover,
            precipitation,
            shortwave_radiation,
            sunshine_duration
        FROM weather
        ORDER BY ABS(
            strftime('%s', timestamp) -
            strftime('%s', ?)
        )
        LIMIT 1
    """, (timestamp,)).fetchone()


def build_features():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    create_table(conn)

    rows = conn.execute("""
        SELECT
            id,
            timestamp,
            pv_kw,
            load_kw,
            grid_import_kw,
            grid_export_kw,
            battery_soc,
            battery_charge_kw,
            battery_discharge_kw
        FROM foxess_live
        ORDER BY id
    """).fetchall()

    added = 0
    updated = 0

    for row in rows:
        dt = parse_timestamp(row["timestamp"])

        if dt is None:
            print("Skipping invalid timestamp:", row["timestamp"])
            continue

        weather = nearest_weather(conn, row["timestamp"])
        tariff = rates_at(dt)

        pv = float(row["pv_kw"] or 0)
        load = float(row["load_kw"] or 0)

        net_load = load - pv
        solar_surplus = max(0, pv - load)

        exists = conn.execute(
            "SELECT 1 FROM ml_features WHERE foxess_id = ?",
            (row["id"],)
        ).fetchone()

        conn.execute("""
        INSERT OR REPLACE INTO ml_features (
            foxess_id,
            timestamp,
            hour,
            minute,
            day_of_week,
            month,
            is_weekend,

            pv_kw,
            load_kw,
            grid_import_kw,
            grid_export_kw,
            battery_soc,
            battery_charge_kw,
            battery_discharge_kw,

            temperature,
            cloud_cover,
            precipitation,
            shortwave_radiation,
            sunshine_duration,

            import_rate,
            export_rate,
            import_period,
            export_period,

            net_load_kw,
            solar_surplus_kw
        )
        VALUES (
            ?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,
            ?,?,?,?,?,
            ?,?,?,?,
            ?,?
        )
        """, (
            row["id"],
            row["timestamp"],
            dt.hour,
            dt.minute,
            dt.weekday(),
            dt.month,
            1 if dt.weekday() >= 5 else 0,

            pv,
            load,
            row["grid_import_kw"],
            row["grid_export_kw"],
            row["battery_soc"],
            row["battery_charge_kw"],
            row["battery_discharge_kw"],

            weather["temperature"] if weather else None,
            weather["cloud_cover"] if weather else None,
            weather["precipitation"] if weather else None,
            weather["shortwave_radiation"] if weather else None,
            weather["sunshine_duration"] if weather else None,

            tariff["import_rate"],
            tariff["export_rate"],
            tariff["import_period"],
            tariff["export_period"],

            net_load,
            solar_surplus
        ))

        if exists:
            updated += 1
        else:
            added += 1

    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM ml_features"
    ).fetchone()[0]

    conn.close()

    print()
    print("ML FEATURE PIPELINE")
    print("===================")
    print("FoxESS records processed:", len(rows))
    print("New feature rows:        ", added)
    print("Updated feature rows:    ", updated)
    print("Total ML feature rows:   ", total)
    print()


if __name__ == "__main__":
    build_features()
