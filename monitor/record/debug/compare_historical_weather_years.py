import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import percentile_linear


ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def require_float(name: str) -> float:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required.")
    return float(value)


def fetch_year(year: int, cutoff: int, timeout: float) -> dict:
    response = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": require_float("WEATHER_LATITUDE"),
            "longitude": require_float("WEATHER_LONGITUDE"),
            "start_date": f"{year}-06-01",
            "end_date": f"{year}-07-{cutoff:02d}",
            "timezone": os.getenv("WEATHER_TIMEZONE", "UTC"),
            "hourly": "global_tilted_irradiance,shortwave_radiation,cloud_cover,precipitation",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def daily_rows(payload: dict) -> list[dict]:
    hourly = payload.get("hourly", {})
    by_day = defaultdict(lambda: {"gti": 0.0, "ghi": 0.0, "cloud": [], "rain": 0.0})
    times = hourly.get("time", [])
    for index, value in enumerate(times):
        target = datetime.fromisoformat(value).date()
        gti = float((hourly.get("global_tilted_irradiance") or [0.0] * len(times))[index] or 0.0)
        ghi = float((hourly.get("shortwave_radiation") or [0.0] * len(times))[index] or 0.0)
        cloud = float((hourly.get("cloud_cover") or [0.0] * len(times))[index] or 0.0)
        rain = float((hourly.get("precipitation") or [0.0] * len(times))[index] or 0.0)
        by_day[target]["gti"] += gti / 1000.0
        by_day[target]["ghi"] += ghi / 1000.0
        by_day[target]["rain"] += rain
        if gti > 20 or ghi > 20:
            by_day[target]["cloud"].append(cloud)
    return [
        {
            "date": target,
            "month": target.month,
            "gti_kwh_m2": values["gti"],
            "ghi_kwh_m2": values["ghi"],
            "daylight_cloud_percent": statistics.mean(values["cloud"]) if values["cloud"] else 0.0,
            "precipitation_mm": values["rain"],
        }
        for target, values in sorted(by_day.items())
    ]


def summarize(rows: list[dict]) -> dict:
    gti = [row["gti_kwh_m2"] for row in rows]
    ghi = [row["ghi_kwh_m2"] for row in rows]
    clouds = [row["daylight_cloud_percent"] for row in rows]
    top = sorted(rows, key=lambda row: row["gti_kwh_m2"], reverse=True)[: min(5, len(rows))]
    return {
        "days": len(rows),
        "total_gti_kwh_m2": round(sum(gti), 2),
        "average_gti_kwh_m2": round(statistics.mean(gti), 3),
        "p90_gti_kwh_m2": round(percentile_linear(gti, 0.90) if gti else 0.0, 3),
        "top_5_average_gti_kwh_m2": round(statistics.mean(row["gti_kwh_m2"] for row in top), 3),
        "total_ghi_kwh_m2": round(sum(ghi), 2),
        "average_daylight_cloud_percent": round(statistics.mean(clouds), 1),
        "total_precipitation_mm": round(sum(row["precipitation_mm"] for row in rows), 1),
    }


def change(current: float, reference: float) -> float | None:
    return round((current / reference - 1) * 100, 1) if reference else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Open-Meteo historical solar weather for matched years.")
    parser.add_argument("--reference-year", type=int, default=2025)
    parser.add_argument("--current-year", type=int, default=2026)
    parser.add_argument("--july-cutoff-day", type=int, default=27)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    years = (args.reference_year, args.current_year)
    rows = {year: daily_rows(fetch_year(year, args.july_cutoff_day, args.timeout)) for year in years}
    periods = {
        str(year): {
            str(month): summarize([row for row in rows[year] if row["month"] == month])
            for month in (6, 7)
        }
        for year in years
    }
    comparisons = {}
    for month in (6, 7):
        reference = periods[str(args.reference_year)][str(month)]
        current = periods[str(args.current_year)][str(month)]
        comparisons[str(month)] = {
            key + "_change_percent": change(current[key], reference[key])
            for key in (
                "total_gti_kwh_m2",
                "average_gti_kwh_m2",
                "p90_gti_kwh_m2",
                "top_5_average_gti_kwh_m2",
                "total_ghi_kwh_m2",
                "average_daylight_cloud_percent",
                "total_precipitation_mm",
            )
        }

    print(json.dumps({"periods": periods, "comparisons": comparisons}, indent=2))


if __name__ == "__main__":
    main()
