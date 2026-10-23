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
    percentile_linear,
    query_measurement_values,
)


SEGMENTS = {
    "early_morning_07_09": (7, 9),
    "late_morning_09_11": (9, 11),
    "midday_11_13": (11, 13),
    "early_afternoon_13_15": (13, 15),
    "late_afternoon_15_17": (15, 17),
}


def change(current: float, reference: float) -> float | None:
    return round((current / reference - 1) * 100, 1) if reference else None


def summarize(values: list[float]) -> dict:
    top = sorted(values, reverse=True)[: min(5, len(values))]
    return {
        "average_kwh": round(statistics.mean(values), 3),
        "p90_kwh": round(percentile_linear(values, 0.90) if values else 0.0, 3),
        "top_5_average_kwh": round(statistics.mean(top), 3),
        "maximum_kwh": round(max(values), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare matched solar-production segments and hourly upper envelopes between two years."
    )
    parser.add_argument("--reference-year", type=int, default=2025)
    parser.add_argument("--current-year", type=int, default=2026)
    parser.add_argument("--july-cutoff-day", type=int, default=27)
    parser.add_argument("--min-intervals", type=int, default=92)
    parser.add_argument("--cohort-days", type=int, default=5)
    args = parser.parse_args()

    local_tz = ZoneInfo(env_timezone("ENPHASE_HISTORY_TIMEZONE", "HISTORY_TIMEZONE"))
    start = datetime.combine(date(args.reference_year, 6, 1), time.min, tzinfo=local_tz)
    stop = datetime.combine(
        date(args.current_year, 7, args.july_cutoff_day) + timedelta(days=1), time.min, tzinfo=local_tz
    )
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
            by_day[target].append((reading.time, max(float(reading.value), 0.0) / 1000.0))

    days = []
    for target, items in sorted(by_day.items()):
        if len(items) < args.min_intervals:
            continue
        hourly = {hour: 0.0 for hour in range(24)}
        quarterly = {slot: 0.0 for slot in range(96)}
        for timestamp, value in items:
            hourly[timestamp.hour] += value
            quarterly[timestamp.hour * 4 + timestamp.minute // 15] += value
        segments = {
            name: sum(hourly[hour] for hour in range(start_hour, stop_hour))
            for name, (start_hour, stop_hour) in SEGMENTS.items()
        }
        days.append(
            {
                "date": target,
                "year": target.year,
                "month": target.month,
                "hourly": hourly,
                "quarterly": quarterly,
                "segments": segments,
            }
        )

    periods = {}
    comparisons = {}
    midday_cohorts = {}
    for year in (args.reference_year, args.current_year):
        periods[str(year)] = {}
        midday_cohorts[str(year)] = {}
        for month in (6, 7):
            selected = [day for day in days if day["year"] == year and day["month"] == month]
            periods[str(year)][str(month)] = {
                "segments": {
                    name: summarize([day["segments"][name] for day in selected]) for name in SEGMENTS
                },
                "hourly": {
                    f"{hour:02d}:00": summarize([day["hourly"][hour] for day in selected])
                    for hour in range(7, 18)
                },
                "quarter_hourly": {
                    f"{slot // 4:02d}:{(slot % 4) * 15:02d}": summarize(
                        [day["quarterly"][slot] for day in selected]
                    )
                    for slot in range(11 * 4, 17 * 4)
                },
            }
            cohort = sorted(
                selected, key=lambda day: day["segments"]["midday_11_13"], reverse=True
            )[: args.cohort_days]
            midday_cohorts[str(year)][str(month)] = {
                "dates": [day["date"].isoformat() for day in cohort],
                "average_hourly_kwh": {
                    f"{hour:02d}:00": round(statistics.mean(day["hourly"][hour] for day in cohort), 3)
                    for hour in range(7, 18)
                },
            }

    for month in (6, 7):
        reference = periods[str(args.reference_year)][str(month)]
        current = periods[str(args.current_year)][str(month)]
        comparisons[str(month)] = {
            "segment_top_5_change_percent": {
                name: change(
                    current["segments"][name]["top_5_average_kwh"],
                    reference["segments"][name]["top_5_average_kwh"],
                )
                for name in SEGMENTS
            },
            "segment_average_change_percent": {
                name: change(
                    current["segments"][name]["average_kwh"],
                    reference["segments"][name]["average_kwh"],
                )
                for name in SEGMENTS
            },
            "hourly_top_5_change_percent": {
                hour: change(
                    current["hourly"][hour]["top_5_average_kwh"],
                    reference["hourly"][hour]["top_5_average_kwh"],
                )
                for hour in current["hourly"]
            },
            "quarter_hourly_top_5_change_percent": {
                slot: change(
                    current["quarter_hourly"][slot]["top_5_average_kwh"],
                    reference["quarter_hourly"][slot]["top_5_average_kwh"],
                )
                for slot in current["quarter_hourly"]
            },
        }

    print(
        json.dumps(
            {
                "scope": {
                    "reference_year": args.reference_year,
                    "current_year": args.current_year,
                    "june_days": "1-30",
                    "july_days": f"1-{args.july_cutoff_day}",
                    "timezone": local_tz.key,
                },
                "periods": periods,
                "comparisons": comparisons,
                "best_midday_cohorts": midday_cohorts,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
