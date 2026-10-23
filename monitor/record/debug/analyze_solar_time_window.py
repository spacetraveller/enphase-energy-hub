import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    DEFAULT_TIMEZONE,
    HISTORY_PRODUCTION_FIELD,
    SLOT_MINUTES,
    env_timezone,
    parse_hhmm,
    percentile,
    query_history_field_by_day_slot,
    season_for_month,
)


def query_window_days(
    start: str,
    stop: str,
    window_start: str,
    window_stop: str,
    timezone_name: str,
) -> list[dict]:
    from zoneinfo import ZoneInfo

    days_by_slot = query_history_field_by_day_slot(
        HISTORY_PRODUCTION_FIELD,
        start=start,
        stop=stop,
        local_tz=ZoneInfo(timezone_name),
        duplicate_slot_mode="sum",
    )
    start_clock = parse_hhmm(window_start)
    stop_clock = parse_hhmm(window_stop)
    days: dict[object, dict] = {}
    for day, slots in days_by_slot.items():
        row = days.setdefault(
            day,
            {
                "date": day.isoformat(),
                "month": day.month,
                "season": season_for_month(day.month),
                "window_wh": 0.0,
                "day_wh": sum(slots.values()),
                "window_rows": 0,
                "day_rows": len(slots),
            },
        )
        for slot, value in slots.items():
            minutes = slot * SLOT_MINUTES
            local_clock = parse_hhmm(f"{minutes // 60:02d}:{minutes % 60:02d}")
            if start_clock <= local_clock < stop_clock:
                row["window_wh"] += value
                row["window_rows"] += 1
    return list(days.values())


def summarize_group(rows: list[dict]) -> dict:
    window_kwh = [row["window_wh"] / 1000.0 for row in rows]
    day_kwh = [row["day_wh"] / 1000.0 for row in rows]
    return {
        "days": len(rows),
        "avg_window_kwh": round(sum(window_kwh) / len(window_kwh), 3),
        "median_window_kwh": round(percentile(window_kwh, 0.5), 3),
        "p90_window_kwh": round(percentile(window_kwh, 0.9), 3),
        "max_window_kwh": round(max(window_kwh), 3),
        "avg_day_kwh": round(sum(day_kwh) / len(day_kwh), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rank historical solar production for a local time-of-day window."
    )
    parser.add_argument("--start", default="2020-01-01T00:00:00Z")
    parser.add_argument("--stop", default="now()")
    parser.add_argument("--window-start", default="15:00")
    parser.add_argument("--window-stop", default="17:00")
    parser.add_argument("--timezone", default=env_timezone("ENPHASE_HISTORY_TIMEZONE", default=DEFAULT_TIMEZONE))
    parser.add_argument("--top-days", type=int, default=30)
    parser.add_argument("--min-day-rows", type=int, default=80)
    parser.add_argument("--min-window-rows", type=int, default=6)
    args = parser.parse_args()

    rows = [
        row
        for row in query_window_days(
            args.start,
            args.stop,
            args.window_start,
            args.window_stop,
            args.timezone,
        )
        if row["day_rows"] >= args.min_day_rows and row["window_rows"] >= args.min_window_rows
    ]
    rows.sort(key=lambda row: row["window_wh"], reverse=True)
    top_rows = rows[: args.top_days]

    by_season = defaultdict(list)
    by_month = defaultdict(list)
    for row in rows:
        by_season[row["season"]].append(row)
        by_month[row["month"]].append(row)

    top_by_season = defaultdict(list)
    top_by_month = defaultdict(list)
    for row in top_rows:
        top_by_season[row["season"]].append(row)
        top_by_month[row["month"]].append(row)

    result = {
        "timezone": args.timezone,
        "window": {"start": args.window_start, "stop": args.window_stop},
        "filters": {
            "min_day_rows": args.min_day_rows,
            "min_window_rows": args.min_window_rows,
            "top_days": args.top_days,
        },
        "dataset": {
            "days": len(rows),
            "first": min(row["date"] for row in rows) if rows else None,
            "last": max(row["date"] for row in rows) if rows else None,
        },
        "all_days_by_season": {
            season: summarize_group(group)
            for season, group in sorted(by_season.items())
        },
        "all_days_by_month": {
            f"{month:02d}": summarize_group(group)
            for month, group in sorted(by_month.items())
        },
        "top_days_by_season": {
            season: {
                "days": len(group),
                "share": round(len(group) / len(top_rows), 3) if top_rows else 0,
                "avg_window_kwh": round(
                    sum(row["window_wh"] for row in group) / len(group) / 1000.0, 3
                ),
            }
            for season, group in sorted(top_by_season.items())
        },
        "top_days_by_month": {
            f"{month:02d}": {
                "days": len(group),
                "share": round(len(group) / len(top_rows), 3) if top_rows else 0,
                "avg_window_kwh": round(
                    sum(row["window_wh"] for row in group) / len(group) / 1000.0, 3
                ),
            }
            for month, group in sorted(top_by_month.items())
        },
        "top_days": [
            {
                "date": row["date"],
                "season": row["season"],
                "month": row["month"],
                "window_kwh": round(row["window_wh"] / 1000.0, 3),
                "day_kwh": round(row["day_wh"] / 1000.0, 3),
            }
            for row in top_rows
        ],
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
