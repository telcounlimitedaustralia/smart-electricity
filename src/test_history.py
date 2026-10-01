import sys
import json
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from foxess import get_device, foxess_post

TZ = ZoneInfo("Australia/Sydney")

# Test a day roughly 3 months ago
TEST_DATE = "2026-05-01"


def timestamp_ms(dt):
    return int(dt.timestamp() * 1000)


def main():
    print()
    print("FOXESS HISTORICAL DATA TEST")
    print("===========================")
    print("Test date:", TEST_DATE)
    print("Mode: READ ONLY")
    print()

    device = get_device()
    sn = device.get("deviceSN")

    if not sn:
        raise RuntimeError("Device serial number missing")

    start = datetime(
        2026, 5, 1, 0, 0, 0,
        tzinfo=TZ
    )

    end = datetime(
        2026, 5, 1, 23, 59, 59,
        tzinfo=TZ
    )

    payload = {
        "sn": sn,
        "begin": timestamp_ms(start),
        "end": timestamp_ms(end),
        "variables": [
            "pvPower",
            "loadsPower",
            "feedinPower",
            "gridConsumptionPower",
            "SoC",
            "batChargePower",
            "batDischargePower"
        ]
    }

    path = "/op/v0/device/history/query"

    print("Requesting:")
    print(start.isoformat())
    print("to")
    print(end.isoformat())
    print()

    data = foxess_post(path, payload)

    print()
    print("FOXESS RESPONSE")
    print("===============")

    if data is None:
        print("No response.")
        return

    print("errno:", data.get("errno"))
    print("message:", data.get("msg"))

    result = data.get("result")

    if result is None:
        print()
        print("No result returned.")
        print()
        print("Raw response:")
        print(json.dumps(data, indent=2)[:5000])
        return

    print()
    print("Historical data IS available.")
    print()

    # Inspect response structure without assuming its exact shape
    if isinstance(result, list):
        print("Result items:", len(result))

        for i, item in enumerate(result[:10]):
            if isinstance(item, dict):
                print(
                    f"Item {i+1}:",
                    item.get("variable")
                    or item.get("name")
                    or list(item.keys())
                )

    elif isinstance(result, dict):
        print("Result keys:")
        for key in result.keys():
            print(" -", key)

    print()
    print("Response sample:")
    print("----------------")
    print(json.dumps(result, indent=2)[:8000])


if __name__ == "__main__":
    main()
