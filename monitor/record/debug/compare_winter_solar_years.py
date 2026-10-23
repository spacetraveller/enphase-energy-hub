import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (
    HISTORY_MEASUREMENT,
    HISTORY_PRODUCTION_FIELD,
    env_timezone,
    flux_time,
    query_measurement_values,
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(days: list[dict]) -> dict:
    totals = [day["production_kwh"] for day in days]
    peaks = [day["peak_kw"] for day in days]
    top_count = min(5, len(days))
    top_days = sorted(days, key=lambda day: day["production_kwh"], reverse=True)[:top_count]
    return {
        "days": len(days),
        "total_kwh": round(sum(totals), 2),
        "average_kwh": round(statistics.mean(totals), 2),
        "median_kwh": round(statistics.median(totals), 2),
        "p90_kwh": round(percentile(totals, 0.90), 2),
        "maximum_kwh": round(max(totals), 2),
        "top_5_average_kwh": round(statistics.mean(day["production_kwh"] for day in top_days), 2),
        "p90_peak_kw": round(percentile(peaks, 0.90), 2),
        "maximum_peak_kw": round(max(peaks), 2),
        "top_days": [
            {
                "date": day["date"].isoformat(),
                "production_kwh": round(day["production_kwh"], 2),
                "peak_kw": round(day["peak_kw"], 2),
                "roughness": round(day["roughness"], 3),
            }
            for day in top_days
        ],
    }


def percent_change(current: float, reference: float) -> float | None:
    if not reference:
        return None
    return round((current / reference - 1) * 100, 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare matched June and partial-July solar production between two years."
    )
    parser.add_argument("--reference-year", type=int, default=2025)
    parser.add_argument("--current-year", type=int, default=2026)
    parser.add_argument("--july-cutoff-day", type=int, default=27)
    parser.add_argument("--min-intervals", type=int, default=92)
    args = parser.parse_args()

    local_tz = ZoneInfo(env_timezone("ENPHASE_HISTORY_TIMEZONE", "HISTORY_TIMEZONE"))
    start = datetime.combine(date(args.reference_year, 6, 1), time.min, tzinfo=local_tz)
    stop = datetime.combine(date(args.current_year, 7, args.july_cutoff_day) + timedelta(days=1), time.min, tzinfo=local_tz)
    readings = query_measurement_values(
        HISTORY_MEASUREMENT,
        field=HISTORY_PRODUCTION_FIELD,
        start=flux_time(start),
        stop=flux_time(stop),
        local_tz=local_tz,
    )

    by_day = defaultdict(list)
    for reading in readings:
        target = reading.time.date()
        if target.year not in (args.reference_year, args.current_year):
            continue
        if target.month == 6 or (target.month == 7 and target.day <= args.july_cutoff_day):
            by_day[target].append(max(reading.value, 0.0) / 1000.0)

    daily = []
    for target, values in sorted(by_day.items()):
        if len(values) < args.min_intervals:
            continue
        daylight = [value for value in values if value > 0.005]
        variation = sum(abs(current - previous) for previous, current in zip(daylight, daylight[1:]))
        total = sum(values)
        daily.append(
            {
                "date": target,
                "year": target.year,
                "month": target.month,
                "intervals": len(values),
                "production_kwh": total,
                "peak_kw": max(values) * 4,
                "roughness": variation / max(total, 0.001),
            }
        )

    thresholds = {}
    for month in (6, 7):
        reference_values = [
            day["production_kwh"]
            for day in daily
            if day["year"] == args.reference_year and day["month"] == month
        ]
        thresholds[month] = {
            "minimum_below_kwh": percentile(reference_values, 0.25),
            "maximum_at_or_above_kwh": percentile(reference_values, 0.75),
        }

    for day in daily:
        threshold = thresholds[day["month"]]
        if day["production_kwh"] < threshold["minimum_below_kwh"]:
            day["category"] = "minimum"
        elif day["production_kwh"] >= threshold["maximum_at_or_above_kwh"]:
            day["category"] = "maximum"
        else:
            day["category"] = "average"

    periods = {}
    for year in (args.reference_year, args.current_year):
        periods[str(year)] = {}
        for month in (6, 7):
            month_days = [day for day in daily if day["year"] == year and day["month"] == month]
            summary = summarize(month_days)
            summary["categories"] = {
                category: sum(day["category"] == category for day in month_days)
                for category in ("maximum", "average", "minimum")
            }
            periods[str(year)][str(month)] = summary

    comparisons = {}
    for month in (6, 7):
        reference = periods[str(args.reference_year)][str(month)]
        current = periods[str(args.current_year)][str(month)]
        comparisons[str(month)] = {
            key + "_change_percent": percent_change(current[key], reference[key])
            for key in (
                "total_kwh",
                "average_kwh",
                "median_kwh",
                "p90_kwh",
                "top_5_average_kwh",
                "p90_peak_kw",
                "maximum_peak_kw",
            )
        }

    print(
        json.dumps(
            {
                "definition": {
                    "minimum": "below the reference year's month-specific 25th percentile",
                    "average": "between the reference year's month-specific 25th and 75th percentiles",
                    "maximum": "at or above the reference year's month-specific 75th percentile",
                    "complete_day_minimum_intervals": args.min_intervals,
                },
                "scope": {
                    "reference_year": args.reference_year,
                    "current_year": args.current_year,
                    "june_days": "1-30",
                    "july_days": f"1-{args.july_cutoff_day}",
                    "timezone": local_tz.key,
                },
                "thresholds": {
                    str(month): {key: round(value, 2) for key, value in threshold.items()}
                    for month, threshold in thresholds.items()
                },
                "periods": periods,
                "comparisons": comparisons,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
