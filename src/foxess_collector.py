import os
import sqlite3
import hashlib
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(".env")

API_KEY = os.getenv("FOXESS_API_KEY")
DB_PATH = "data/energy.db"
TZ = ZoneInfo("Australia/Sydney")
BASE_URL = "https://www.foxesscloud.com"

if not API_KEY:
    raise RuntimeError("FOXESS_API_KEY not found in .env")


def foxess_headers(path):
    timestamp = str(int(time.time() * 1000))
    signature_text = f"{path}\\r\\n{API_KEY}\\r\\n{timestamp}"
    signature = hashlib.md5(signature_text.encode()).hexdigest()

    return {
        "token": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "lang": "en",
        "Content-Type": "application/json",
    }


def post(path, payload):
    response = requests.post(
        BASE_URL + path,
        headers=foxess_headers(path),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    if data.get("errno") != 0:
        raise RuntimeError(f"FoxESS API error: {data}")

    return data


def get_device():
    path = "/op/v0/device/list"

    data = post(
        path,
        {
            "currentPage": 1,
            "pageSize": 20,
        },
    )

    devices = data.get("result", {}).get("data", [])

    if not devices:
        raise RuntimeError("No FoxESS device found")

    return devices[0]


def get_live_data(device_sn):
    path = "/op/v0/device/real/query"

    data = post(
        path,
        {
            "sn": device_sn,
            "variables": [],
        },
    )

    result = data.get("result", [])

    if not result:
        raise RuntimeError("No live FoxESS data returned")

    readings = result[0].get("datas", [])

    return {
        item.get("variable"): item.get("value")
        for item in readings
    }


def value(data, *names):
    for name in names:
        if name in data and data[name] is not None:
            try:
                return float(data[name])
            except (TypeError, ValueError):
                return data[name]
    return None


def save_reading(device_sn, data):
    os.makedirs("data", exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS foxess_live (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            device_sn TEXT NOT NULL,
            pv_kw REAL,
            load_kw REAL,
            grid_import_kw REAL,
            grid_export_kw REAL,
            battery_soc REAL,
            battery_charge_kw REAL,
            battery_discharge_kw REAL,
            battery_temperature REAL,
            inverter_temperature REAL
        )
        """
    )

    # Store one explicit business timezone regardless of the VM's OS timezone.
    timestamp = datetime.now(TZ).isoformat(timespec="seconds")

    row = (
        timestamp,
        device_sn,
        value(data, "pvPower"),
        value(data, "loadsPower"),
        value(data, "gridConsumptionPower"),
        value(data, "feedinPower"),
        value(data, "SoC", "SoC_1"),
        value(data, "batChargePower"),
        value(data, "batDischargePower"),
        value(data, "batTemperature", "batTemperature_1"),
        value(data, "invTemperation"),
    )

    conn.execute(
        """
        INSERT INTO foxess_live (
            timestamp,
            device_sn,
            pv_kw,
            load_kw,
            grid_import_kw,
            grid_export_kw,
            battery_soc,
            battery_charge_kw,
            battery_discharge_kw,
            battery_temperature,
            inverter_temperature
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )

    conn.commit()
    conn.close()

    return row


def main():
    device = get_device()
    device_sn = device.get("deviceSN")

    if not device_sn:
        raise RuntimeError("Device serial number missing")

    print(f"Device: {device.get('deviceType')} ({device_sn})")

    data = get_live_data(device_sn)

    row = save_reading(device_sn, data)

    print()
    print("FOXESS READING SAVED")
    print("====================")
    print(f"Time:              {row[0]}")
    print(f"PV:                {row[2]} kW")
    print(f"House load:        {row[3]} kW")
    print(f"Grid import:       {row[4]} kW")
    print(f"Grid export:       {row[5]} kW")
    print(f"Battery SOC:       {row[6]} %")
    print(f"Battery charging:  {row[7]} kW")
    print(f"Battery discharge: {row[8]} kW")
    print()
    print(f"Saved to: {DB_PATH}")


if __name__ == "__main__":
    main()
