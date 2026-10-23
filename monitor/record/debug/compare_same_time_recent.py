import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    ENERGY_FLOW_MEASUREMENT,
    env_timezone,
    flux_tag_filters,
    flux_time,
    percentile_linear,
    percentile_rank,
    query_measurement_values,
    same_time_comparisons,
)


def summarize(values: list[float], target: float) -> dict:
    return {
        "comparison_count": len(values),
        "target_w": round(target, 3),
        "percentile_rank": round(percentile_rank(values, target) * 100, 1),
        "min_w": round(min(values), 3) if values else None,
        "p25_w": round(percentile_linear(values, 0.25), 3) if values else None,
        "median_w": round(percentile_linear(values, 0.5), 3) if values else None,
        "p75_w": round(percentile_linear(values, 0.75), 3) if values else None,
        "p90_w": round(percentile_linear(values, 0.9), 3) if values else None,
        "max_w": round(max(values), 3) if values else None,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare today's latest readings with readings from the same local "
            "time-of-day over a recent lookback window."
        )
    )
    parser.add_argument("--measurement", default=ENERGY_FLOW_MEASUREMENT)
    parser.add_argument("--field", default="w_now")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--recent-count", type=int, default=2)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--same-time-window-minutes", type=int, default=5)
    args = parser.parse_args(argv)

    local_tz = ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
    now = datetime.now(local_tz)
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=local_tz)
    lookback_start = today_start - timedelta(days=args.lookback_days)
    tags = args.tag or ["section=production", "type=eim"]
    try:
        tag_filters = flux_tag_filters(tags)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    readings = query_measurement_values(
        args.measurement,
        field=args.field,
        start=flux_time(lookback_start),
        stop=flux_time(now),
        filters=tag_filters,
        local_tz=local_tz,
    )
    today_readings, comparison_rows = same_time_comparisons(
        readings,
        local_tz=local_tz,
        target_date=now.date(),
        recent_count=args.recent_count,
        window_minutes=args.same_time_window_minutes,
    )

    comparisons = []
    for comparison in comparison_rows:
        values = [row.value for row in comparison.matches]
        summary = summarize(values, comparison.target.value)
        summary.update(
            {
                "target_time": comparison.target.time.isoformat(),
                "same_time_window_minutes": args.same_time_window_minutes,
                "matching_dates": len({row.time.date() for row in comparison.matches}),
            }
        )
        comparisons.append(summary)

    print(
        json.dumps(
            {
                "now": now.isoformat(),
                "measurement": args.measurement,
                "field": args.field,
                "tags": tags,
                "lookback_days": args.lookback_days,
                "today_readings": len(today_readings),
                "comparisons": comparisons,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
