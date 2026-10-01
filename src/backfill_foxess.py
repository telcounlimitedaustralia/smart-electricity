import sys
import time
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from foxess import get_device, foxess_post

DB_PATH = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")

# Historical database begins from May 2026.
START_DATE = datetime(2026, 5, 1, tzinfo=TZ).date()

# Only download completed calendar days.
# This automatically advances every day.
END_DATE = datetime.now(TZ).date() - timedelta(days=1)

VARIABLES = [
    "pvPower",
    "loadsPower",
    "feedinPower",
    "gridConsumptionPower",
    "SoC",
    "SoC_1",
    "batChargePower",
    "batDischargePower",
]


def create_table(conn):

    conn.execute("""
        CREATE TABLE IF NOT EXISTS foxess_history (
            timestamp TEXT PRIMARY KEY,
            device_sn TEXT,

            pv_kw REAL,
            load_kw REAL,

            grid_export_kw REAL,
            grid_import_kw REAL,

            battery_soc REAL,
            battery_soc_1 REAL,

            battery_charge_kw REAL,
            battery_discharge_kw REAL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_foxess_history_timestamp
        ON foxess_history(timestamp)
    """)

    conn.commit()


def parse_time(value):

    # Example:
    # 2026-05-01 00:03:17 AEST+1000

    try:
        clean = value.rsplit(" ", 1)[0]

        dt = datetime.strptime(
            clean,
            "%Y-%m-%d %H:%M:%S"
        )

        dt = dt.replace(tzinfo=TZ)

        return dt.isoformat()

    except Exception:
        return value


def download_day(sn, day):

    start = datetime(
        day.year,
        day.month,
        day.day,
        0, 0, 0,
        tzinfo=TZ
    )

    end = start + timedelta(days=1) - timedelta(seconds=1)

    payload = {
        "sn": sn,
        "begin": int(start.timestamp() * 1000),
        "end": int(end.timestamp() * 1000),
        "variables": VARIABLES
    }

    result = foxess_post(
        "/op/v0/device/history/query",
        payload
    )

    if not result:
        raise RuntimeError("No response")

    if result.get("errno") != 0:
        raise RuntimeError(
            f"FoxESS error: {result.get('msg')}"
        )

    return result.get("result", [])


def transform(result):

    rows = {}

    mapping = {
        "pvPower": "pv_kw",
        "loadsPower": "load_kw",
        "feedinPower": "grid_export_kw",
        "gridConsumptionPower": "grid_import_kw",
        "SoC": "battery_soc",
        "SoC_1": "battery_soc_1",
        "batChargePower": "battery_charge_kw",
        "batDischargePower": "battery_discharge_kw",
    }

    for device in result:

        sn = device.get("deviceSN")

        for variable in device.get("datas", []):

            name = variable.get("variable")

            if name not in mapping:
                continue

            target = mapping[name]

            # FoxESS response uses "data"
            samples = variable.get("data", [])

            for sample in samples:

                timestamp = parse_time(
                    sample.get("time")
                )

                if timestamp not in rows:
                    rows[timestamp] = {
                        "timestamp": timestamp,
                        "device_sn": sn,
                        "pv_kw": None,
                        "load_kw": None,
                        "grid_export_kw": None,
                        "grid_import_kw": None,
                        "battery_soc": None,
                        "battery_soc_1": None,
                        "battery_charge_kw": None,
                        "battery_discharge_kw": None,
                    }

                rows[timestamp][target] = sample.get("value")

    return list(rows.values())


def insert_rows(conn, rows):

    sql = """
        INSERT OR REPLACE INTO foxess_history (
            timestamp,
            device_sn,
            pv_kw,
            load_kw,
            grid_export_kw,
            grid_import_kw,
            battery_soc,
            battery_soc_1,
            battery_charge_kw,
            battery_discharge_kw
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    values = []

    for r in rows:

        values.append((
            r["timestamp"],
            r["device_sn"],
            r["pv_kw"],
            r["load_kw"],
            r["grid_export_kw"],
            r["grid_import_kw"],
            r["battery_soc"],
            r["battery_soc_1"],
            r["battery_charge_kw"],
            r["battery_discharge_kw"],
        ))

    conn.executemany(sql, values)
    conn.commit()


def main():

    print()
    print("FOXESS HISTORICAL BACKFILL")
    print("==========================")
    print("From:", START_DATE)
    print("To:  ", END_DATE)
    print()

    device = get_device()
    sn = device["deviceSN"]

    conn = sqlite3.connect(DB_PATH)

    create_table(conn)

    day = START_DATE

    total_days = (END_DATE - START_DATE).days + 1
    completed = 0

    while day <= END_DATE:

        completed += 1

        existing = conn.execute("""
            SELECT COUNT(*)
            FROM foxess_history
            WHERE substr(timestamp,1,10) = ?
        """, (day.isoformat(),)).fetchone()[0]

        if existing >= 250:

            print(
                f"[{completed}/{total_days}] "
                f"{day} already exists "
                f"({existing} rows) - SKIP"
            )

            day += timedelta(days=1)
            continue

        print(
            f"[{completed}/{total_days}] "
            f"Downloading {day}..."
        )

        success = False

        for attempt in range(1, 4):

            try:

                result = download_day(sn, day)

                rows = transform(result)

                insert_rows(conn, rows)

                print(
                    f"    saved {len(rows)} readings"
                )

                success = True
                break

            except Exception as e:

                print(
                    f"    attempt {attempt} failed:",
                    e
                )

                if attempt < 3:
                    time.sleep(10)

        if not success:

            print(
                "    FAILED - continue to next day"
            )

        # Be gentle with FoxESS API
        time.sleep(1.5)

        day += timedelta(days=1)

    print()
    print("BACKFILL COMPLETE")
    print("=================")

    count = conn.execute("""
        SELECT COUNT(*)
        FROM foxess_history
    """).fetchone()[0]

    first = conn.execute("""
        SELECT MIN(timestamp)
        FROM foxess_history
    """).fetchone()[0]

    last = conn.execute("""
        SELECT MAX(timestamp)
        FROM foxess_history
    """).fetchone()[0]

    print("Total readings:", count)
    print("First reading: ", first)
    print("Last reading:  ", last)

    conn.close()


if __name__ == "__main__":
    main()
