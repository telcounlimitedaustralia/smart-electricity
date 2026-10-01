import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "smart-electricity" / "data" / "energy.db"

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)

def initialise():
    con = connect()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS weather (
        timestamp TEXT PRIMARY KEY,
        temperature REAL,
        apparent_temperature REAL,
        cloud_cover REAL,
        precipitation REAL,
        wind_speed REAL,
        shortwave_radiation REAL,
        direct_radiation REAL,
        diffuse_radiation REAL,
        sunshine_duration REAL,
        source TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS energy (
        timestamp TEXT PRIMARY KEY,
        pv_generation_kw REAL,
        load_kw REAL,
        battery_soc REAL,
        battery_power_kw REAL,
        grid_power_kw REAL,
        grid_import_kw REAL,
        grid_export_kw REAL,
        source TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS forecasts (
        timestamp TEXT,
        forecast_created TEXT,
        solar_kwh REAL,
        load_kwh REAL,
        model_version TEXT,
        PRIMARY KEY(timestamp, forecast_created)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS decisions (
        timestamp TEXT PRIMARY KEY,
        battery_soc REAL,
        predicted_solar_kwh REAL,
        predicted_load_kwh REAL,
        import_rate REAL,
        export_rate REAL,
        action TEXT,
        target_soc REAL,
        discharge_kw REAL,
        reason TEXT,
        executed INTEGER DEFAULT 0
    )
    """)

    con.commit()
    con.close()

    print(f"Database ready: {DB_PATH}")

if __name__ == "__main__":
    initialise()
