import os
import time
import hashlib
import requests
import json
from dotenv import load_dotenv

load_dotenv(".env")

TOKEN = os.getenv("FOXESS_API_KEY")
BASE_URL = "https://www.foxesscloud.com"

if not TOKEN:
    raise RuntimeError("FOXESS_API_KEY missing from .env")


def auth_headers(path):
    timestamp = str(round(time.time() * 1000))

    sign_text = f"{path}\\r\\n{TOKEN}\\r\\n{timestamp}"
    signature = hashlib.md5(sign_text.encode("utf-8")).hexdigest()

    return {
        "Token": TOKEN,
        "Timestamp": timestamp,
        "Signature": signature,
        "Lang": "en",
        "Timezone": "Australia/Sydney",
        "Content-Type": "application/json",
    }


def foxess_post(path, payload):
    response = requests.post(
        BASE_URL + path,
        headers=auth_headers(path),
        json=payload,
        timeout=30
    )

    print("HTTP status:", response.status_code)

    try:
        return response.json()
    except Exception:
        print("Non-JSON response:")
        print(response.text[:1000])
        return None


def get_device():
    path = "/op/v0/device/list"

    data = foxess_post(
        path,
        {
            "currentPage": 1,
            "pageSize": 20
        }
    )

    if not data:
        raise RuntimeError("No response from FoxESS")

    if data.get("errno") != 0:
        raise RuntimeError(
            f"FoxESS device error: {data.get('msg')}"
        )

    devices = data.get("result", {}).get("data", [])

    if not devices:
        raise RuntimeError("No FoxESS devices found")

    device = devices[0]

    print()
    print("Device found:")
    print("  Type:", device.get("deviceType"))
    print("  Status:", device.get("status"))

    return device


def get_live_data(device_sn):
    path = "/op/v1/device/real/query"

    data = foxess_post(
        path,
        {
            "sns": [device_sn]
        }
    )

    print()
    print("LIVE FOXESS DATA")
    print("=================")

    if data is None:
        return

    # Print formatted response so we can identify the exact
    # variable names returned by your H3 inverter.
    print(json.dumps(data, indent=2))


def main():
    device = get_device()

    device_sn = device.get("deviceSN")

    if not device_sn:
        raise RuntimeError("Device serial number missing")

    print()
    print("Requesting live inverter data...")

    get_live_data(device_sn)


if __name__ == "__main__":
    main()
