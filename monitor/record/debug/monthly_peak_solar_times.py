import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    DEFAULT_TIMEZONE,
    HISTORY_PRODUCTION_FIELD,
    SLOT_MINUTES,
    SLOTS_PER_DAY,
    minutes_to_hhmm,
    query_history_field_by_day_slot,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize average monthly solar peak times from 15-minute Enphase history."
        )
    )
    parser.add_argument(
        "--start",
        default=os.getenv("MONTHLY_PEAK_SOLAR_START", "2020-01-01T00:00:00Z"),
        help="Flux start time. Defaults to all available imported history.",
    )
    parser.add_argument(
        "--timezone",
        default=os.getenv("ENPHASE_HISTORY_TIMEZONE", DEFAULT_TIMEZONE),
        help="Local timezone for month and time-of-day grouping.",
    )
    parser.add_argument(
        "--zone-threshold",
        type=float,
        default=0.80,
        help="Fraction of monthly average peak used for the peak zone.",
    )
    parser.add_argument(
        "--min-day-rows",
        type=int,
        default=80,
        help="Minimum 15-minute rows required for a day to be included.",
    )
    parser.add_argument(
        "--min-day-kwh",
        type=float,
        default=1.0,
        help="Minimum daily solar kWh required for a day to be included.",
    )
    return parser.parse_args()


def slot_label(slot: int) -> str:
    return minutes_to_hhmm(slot * SLOT_MINUTES)


def query_rows(start: str, local_tz: ZoneInfo) -> dict[object, dict[int, float]]:
    return {
        day: {slot: value / 1000.0 for slot, value in slots.items()}
        for day, slots in query_history_field_by_day_slot(
            HISTORY_PRODUCTION_FIELD,
            start=start,
            local_tz=local_tz,
        ).items()
    }


def average_monthly_curves(
    days: dict[object, dict[int, float]],
    min_day_rows: int,
    min_day_kwh: float,
) -> tuple[dict[int, list[float]], dict[int, list[float]], dict[int, int], object, object]:
    slot_values = {month: [[] for _ in range(SLOTS_PER_DAY)] for month in range(1, 13)}
    daily_totals: dict[int, list[float]] = defaultdict(list)
    day_counts: dict[int, int] = defaultdict(int)
    included_dates = []

    for day, slots in sorted(days.items()):
        total = sum(slots.values())
        if len(slots) < min_day_rows or total < min_day_kwh:
            continue
        month = day.month
        for slot in range(SLOTS_PER_DAY):
            slot_values[month][slot].append(slots.get(slot, 0.0))
        daily_totals[month].append(total)
        day_counts[month] += 1
        included_dates.append(day)

    curves = {}
    for month, values_by_slot in slot_values.items():
        if day_counts[month] == 0:
            continue
        curves[month] = [
            sum(values) / len(values) if values else 0.0
            for values in values_by_slot
        ]

    first = min(included_dates) if included_dates else None
    last = max(included_dates) if included_dates else None
    return curves, daily_totals, day_counts, first, last


def contiguous_peak_zone(curve: list[float], peak_slot: int, threshold_kwh: float) -> tuple[int, int]:
    start = peak_slot
    while start > 0 and curve[start - 1] >= threshold_kwh:
        start -= 1
    stop = peak_slot
    while stop + 1 < len(curve) and curve[stop + 1] >= threshold_kwh:
        stop += 1
    return start, stop


def summarize_month(
    month: int,
    curve: list[float],
    daily_totals: list[float],
    day_count: int,
    zone_threshold: float,
) -> dict[str, object]:
    peak_slot, peak_kwh = max(enumerate(curve), key=lambda item: item[1])
    peak_kw = peak_kwh * 4.0
    zone_start, zone_stop = contiguous_peak_zone(curve, peak_slot, peak_kwh * zone_threshold)
    total = sum(curve)
    weighted_minutes = (
        sum((slot * SLOT_MINUTES + SLOT_MINUTES / 2) * value for slot, value in enumerate(curve))
        / total
        if total > 0
        else 0.0
    )
    zone_center_minutes = (
        ((zone_start * SLOT_MINUTES) + ((zone_stop + 1) * SLOT_MINUTES)) / 2
    )
    daylight_slots = [slot for slot, value in enumerate(curve) if value >= 0.005]
    daylight_start = slot_label(daylight_slots[0]) if daylight_slots else ""
    daylight_stop = slot_label(daylight_slots[-1] + 1) if daylight_slots else ""

    return {
        "month": month,
        "days": day_count,
        "avg_daily_kwh": sum(daily_totals) / len(daily_totals),
        "solar_midpoint": minutes_to_hhmm(weighted_minutes),
        "peak_slot": slot_label(peak_slot),
        "peak_kw": peak_kw,
        "peak_zone_start": slot_label(zone_start),
        "peak_zone_stop": slot_label(zone_stop + 1),
        "peak_zone_center": minutes_to_hhmm(zone_center_minutes),
        "daylight_start": daylight_start,
        "daylight_stop": daylight_stop,
    }


def main() -> None:
    args = parse_args()
    local_tz = ZoneInfo(args.timezone)
    days = query_rows(args.start, local_tz)
    curves, daily_totals, day_counts, first, last = average_monthly_curves(
        days,
        args.min_day_rows,
        args.min_day_kwh,
    )

    print(
        "DATASET first={first} last={last} timezone={timezone} min_day_rows={rows} min_day_kwh={kwh} peak_zone_threshold={threshold:.0%}".format(
            first=first,
            last=last,
            timezone=args.timezone,
            rows=args.min_day_rows,
            kwh=args.min_day_kwh,
            threshold=args.zone_threshold,
        )
    )
    print(
        "month,days,avg_daily_kwh,solar_midpoint,peak_slot,peak_kw,peak_zone,peak_zone_center,daylight"
    )
    for month in range(1, 13):
        curve = curves.get(month)
        if not curve:
            continue
        summary = summarize_month(
            month,
            curve,
            daily_totals[month],
            day_counts[month],
            args.zone_threshold,
        )
        month_name = datetime(2000, month, 1, tzinfo=timezone.utc).strftime("%b")
        print(
            "{month_name},{days},{avg_daily_kwh:.2f},{solar_midpoint},{peak_slot},{peak_kw:.2f},{peak_zone_start}-{peak_zone_stop},{peak_zone_center},{daylight_start}-{daylight_stop}".format(
                month_name=month_name,
                **summary,
            )
        )


if __name__ == "__main__":
    main()
