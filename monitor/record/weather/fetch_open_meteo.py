import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = [
    "temperature_2m",
    "cloud_cover",
    "precipitation",
    "precipitation_probability",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "global_tilted_irradiance",
    "weather_code",
]


def require_float(name: str) -> float:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required for Open-Meteo forecast collection.")
    return float(value)


def fetch_forecast() -> dict[str, Any]:
    latitude = require_float("WEATHER_LATITUDE")
    longitude = require_float("WEATHER_LONGITUDE")
    timezone_name = os.getenv("WEATHER_TIMEZONE", "UTC")
    # Radiation at next midnight closes the preceding day's final hour.
    required_days = (int(os.getenv("PREDICTION_FIRST_OFFSET_DAYS", "1"))
                     + int(os.getenv("PREDICTION_FORECAST_DAYS", "3")) + 1)
    forecast_days = max(int(os.getenv("WEATHER_FORECAST_DAYS", "4")), required_days)
    timeout = float(os.getenv("WEATHER_TIMEOUT_SECONDS", "15"))

    response = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone_name,
            "forecast_days": forecast_days,
            "hourly": ",".join(HOURLY_FIELDS),
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    payload["_retrieved_at"] = datetime.now(timezone.utc).isoformat()
    payload["_radiation_time_convention"] = "preceding_hour_mean"
    return payload


def write_snapshot(payload: dict[str, Any]) -> None:
    snapshot_dir = Path(os.getenv("WEATHER_SNAPSHOT_DIR", "/app/record/weather/snapshots"))
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    snapshot_path = snapshot_dir / f"{timestamp}.json"
    latest_path = snapshot_dir / "latest.json"
    text = json.dumps(payload, indent=2, sort_keys=True)
    with snapshot_path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    temporary = latest_path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(latest_path)


def points_from_forecast(payload: dict[str, Any]) -> list[Point]:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    timezone_name = payload.get("timezone", os.getenv("WEATHER_TIMEZONE", "UTC"))
    fetched_at = datetime.now(timezone.utc).isoformat()
    points: list[Point] = []

    for index, value in enumerate(times):
        forecast_time = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone_name))
        point = (
            Point("weather_forecast")
            .tag("provider", "open-meteo")
            .tag("timezone", timezone_name)
            .tag("forecast_for", forecast_time.date().isoformat())
            .field("fetched_at_unix", int(datetime.now(timezone.utc).timestamp()))
            .field("fetched_at", fetched_at)
            .time(forecast_time, WritePrecision.NS)
        )
        for field in HOURLY_FIELDS:
            values = hourly.get(field)
            if values is None or index >= len(values):
                continue
            measurement = values[index]
            if measurement is None:
                continue
            point.field(field, float(measurement))
        points.append(point)
    return points


def write_influx(points: list[Point]) -> None:
    if not points:
        raise RuntimeError("Open-Meteo returned no hourly forecast points.")
    org = os.environ["INFLUXDB_ORG"]
    bucket = os.environ["INFLUXDB_BUCKET"]
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=org,
    )
    write_api = client.write_api(write_options=SYNCHRONOUS)
    write_api.write(bucket=bucket, org=org, record=points)
    client.close()


def main() -> None:
    payload = fetch_forecast()
    write_snapshot(payload)
    points = points_from_forecast(payload)
    write_influx(points)
    print(
        json.dumps(
            {
                "provider": "open-meteo",
                "measurement": "weather_forecast",
                "points_written": len(points),
                "first": payload.get("hourly", {}).get("time", [None])[0],
                "last": payload.get("hourly", {}).get("time", [None])[-1],
                "timezone": payload.get("timezone"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
