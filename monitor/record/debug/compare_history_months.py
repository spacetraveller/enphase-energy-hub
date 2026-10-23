import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    DEFAULT_TIMEZONE,
    env_timezone,
    flux_time,
    query_history_field_by_day_slot,
    slot_label,
)


FIELDS = {
    "produced": "energy_produced_wh",
    "consumed": "energy_consumed_wh",
    "exported": "exported_to_grid_wh",
    "imported": "imported_from_grid_wh",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two calendar months of Enphase history.")
    parser.add_argument("--month", required=True, help="Primary month in YYYY-MM format.")
    parser.add_argument("--compare-month", required=True, help="Comparison month in YYYY-MM format.")
    parser.add_argument(
        "--timezone",
        default=env_timezone("ENPHASE_HISTORY_TIMEZONE", default=DEFAULT_TIMEZONE),
    )
    return parser.parse_args()


def month_range(value: str, local_tz: ZoneInfo) -> tuple[date, date, str, str]:
    start_day = date.fromisoformat(f"{value}-01")
    stop_day = (start_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=local_tz)
    stop = datetime.combine(stop_day, datetime.min.time(), tzinfo=local_tz)
    return start_day, stop_day, flux_time(start), flux_time(stop)


def month_summary(value: str, local_tz: ZoneInfo) -> dict[str, object]:
    start_day, stop_day, start, stop = month_range(value, local_tz)
    daily: dict[date, dict[str, float]] = defaultdict(dict)
    slots: dict[str, dict[date, dict[int, float]]] = {}
    for label, field in FIELDS.items():
        rows = query_history_field_by_day_slot(field, start, local_tz, stop=stop)
        slots[label] = rows
        for day, values in rows.items():
            daily[day][label] = sum(values.values()) / 1000.0

    expected_days = (stop_day - start_day).days
    complete_days = [day for day in sorted(daily) if all(len(slots[name].get(day, {})) == 96 for name in FIELDS)]
    totals = {name: sum(daily[day].get(name, 0.0) for day in complete_days) for name in FIELDS}
    production_days = [(day, daily[day]["produced"]) for day in complete_days]
    consumption_days = [(day, daily[day]["consumed"]) for day in complete_days]
    self_consumed = max(0.0, totals["produced"] - totals["exported"])
    solar_fraction = self_consumed / totals["consumed"] if totals["consumed"] else 0.0
    self_consumption = self_consumed / totals["produced"] if totals["produced"] else 0.0

    production_by_slot: dict[int, float] = defaultdict(float)
    for day in complete_days:
        for slot, wh in slots["produced"][day].items():
            production_by_slot[slot] += wh
    peak_slot = max(production_by_slot, key=production_by_slot.get) if production_by_slot else None

    return {
        "month": value,
        "expected_days": expected_days,
        "complete_days": len(complete_days),
        "totals_kwh": {name: round(total, 3) for name, total in totals.items()},
        "daily_average_kwh": {name: round(total / len(complete_days), 3) for name, total in totals.items()} if complete_days else {},
        "median_daily_production_kwh": round(statistics.median(value for _, value in production_days), 3),
        "production_stddev_kwh": round(statistics.pstdev(value for _, value in production_days), 3),
        "best_production_days": [
            {"date": day.isoformat(), "kwh": round(kwh, 3)}
            for day, kwh in sorted(production_days, key=lambda item: item[1], reverse=True)[:5]
        ],
        "worst_production_days": [
            {"date": day.isoformat(), "kwh": round(kwh, 3)}
            for day, kwh in sorted(production_days, key=lambda item: item[1])[:5]
        ],
        "highest_consumption_days": [
            {"date": day.isoformat(), "kwh": round(kwh, 3)}
            for day, kwh in sorted(consumption_days, key=lambda item: item[1], reverse=True)[:5]
        ],
        "self_consumed_solar_kwh": round(self_consumed, 3),
        "self_consumption_pct": round(self_consumption * 100, 1),
        "solar_fraction_of_consumption_pct": round(solar_fraction * 100, 1),
        "grid_independence_pct": round((1.0 - totals["imported"] / totals["consumed"]) * 100, 1) if totals["consumed"] else 0.0,
        "net_grid_kwh": round(totals["imported"] - totals["exported"], 3),
        "average_curve_peak_slot": slot_label(peak_slot) if peak_slot is not None else None,
    }


def main() -> None:
    args = parse_args()
    local_tz = ZoneInfo(args.timezone)
    primary = month_summary(args.month, local_tz)
    comparison = month_summary(args.compare_month, local_tz)
    changes = {}
    for field in FIELDS:
        current = primary["totals_kwh"][field]
        previous = comparison["totals_kwh"][field]
        changes[field] = {
            "kwh": round(current - previous, 3),
            "pct": round((current / previous - 1.0) * 100, 1) if previous else None,
        }
    print(json.dumps({"primary": primary, "comparison": comparison, "changes": changes}, indent=2))


if __name__ == "__main__":
    main()
