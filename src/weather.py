import requests
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / "smart-electricity" / "data" / "energy.db"

# Marsden Park, NSW - approximate suburb-level coordinates
LATITUDE = -33.70
LONGITUDE = 150.84

URL = "https://api.open-meteo.com/v1/forecast"

PARAMS = {
    "latitude": LATITUDE,
    "longitude": LONGITUDE,
    "hourly": [
        "temperature_2m",
        "apparent_temperature",
        "cloud_cover",
        "precipitation",
        "wind_speed_10m",
        "shortwave_radiation",
        "direct_radiation",
        "diffuse_radiation",
        "sunshine_duration",
    ],
    "forecast_days": 7,
    "timezone": "Australia/Sydney",
}

def collect_weather():
    print("Downloading weather forecast...")

    response = requests.get(URL, params=PARAMS, timeout=30)
    response.raise_for_status()

    data = response.json()
    hourly = data["hourly"]

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    count = 0

    for i, timestamp in enumerate(hourly["time"]):
        cur.execute(
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
                "open-meteo",
            ),
        )

        count += 1

    con.commit()
    con.close()

    print(f"Saved {count} hourly weather records.")
    print(f"Forecast downloaded at: {datetime.now(timezone.utc).isoformat()}")

if __name__ == "__main__":
    collect_weather()
