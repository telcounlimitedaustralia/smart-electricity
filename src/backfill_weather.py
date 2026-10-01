import requests
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path.home() / "smart-electricity" / "data" / "energy.db"

LATITUDE = -33.70
LONGITUDE = 150.84

URL = "https://archive-api.open-meteo.com/v1/archive"

START_DATE = date(2026, 5, 1)
END_DATE = date(2026, 9, 26)

HOURLY = [
    "temperature_2m",
    "apparent_temperature",
    "cloud_cover",
    "precipitation",
    "wind_speed_10m",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "sunshine_duration",
]


def fetch_period(start_date, end_date):
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": HOURLY,
        "timezone": "Australia/Sydney",
    }

    print(
        f"Downloading {start_date} -> {end_date}..."
    )

    r = requests.get(
        URL,
        params=params,
        timeout=60
    )

    r.raise_for_status()

    return r.json()["hourly"]


def save_hourly(conn, hourly):
    count = 0

    for i, timestamp in enumerate(hourly["time"]):

        conn.execute(
            """
            INSERT OR REPLACE INTO weather (
                timestamp,
                temperature,
                apparent_temperature,
                cloud_cover,
                precipitation,
                wind_speed,
                shortwave_radiation,
                direct_radiation,
                diffuse_radiation,
                sunshine_duration,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                hourly["temperature_2m"][i],
                hourly["apparent_temperature"][i],
                hourly["cloud_cover"][i],
                hourly["precipitation"][i],
                hourly["wind_speed_10m"][i],
                hourly["shortwave_radiation"][i],
                hourly["direct_radiation"][i],
                hourly["diffuse_radiation"][i],
                hourly["sunshine_duration"][i],
                "open-meteo-archive",
            )
        )

        count += 1

    return count


def main():
    conn = sqlite3.connect(DB_PATH)

    total = 0

    # Download one month at a time.
    # Keeps requests reasonably small and makes failures easy to retry.
    current = START_DATE

    while current <= END_DATE:

        next_month = (
            current.replace(day=28)
            + timedelta(days=4)
        ).replace(day=1)

        chunk_end = min(
            next_month - timedelta(days=1),
            END_DATE
        )

        try:
            hourly = fetch_period(
                current,
                chunk_end
            )

            count = save_hourly(
                conn,
                hourly
            )

            conn.commit()

            total += count

            print(
                f"Saved {count} hourly records."
            )

        except Exception as e:

            print(
                f"ERROR {current} -> "
                f"{chunk_end}: {e}"
            )

            conn.rollback()

            conn.close()
            raise

        current = chunk_end + timedelta(days=1)

    conn.close()

    print()
    print("HISTORICAL WEATHER BACKFILL")
    print("===========================")
    print("Start:", START_DATE)
    print("End:  ", END_DATE)
    print("Rows: ", total)


if __name__ == "__main__":
    main()
