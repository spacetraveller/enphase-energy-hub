import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    DEFAULT_TIMEZONE,
    env_timezone,
    flux_time,
    influx_bucket,
    influx_client,
    influx_org,
    query_measurement_rows,
    query_rows,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize stored EV charger telemetry.")
    parser.add_argument("--month", help="Optional local month in YYYY-MM format.")
    args = parser.parse_args()

    local_tz = ZoneInfo(env_timezone("HISTORY_TIMEZONE", default=DEFAULT_TIMEZONE))
    if args.month:
        start = date.fromisoformat(f"{args.month}-01")
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        range_clause = (
            f"|> range(start: {flux_time(start_date_time(start, local_tz))}, "
            f"stop: {flux_time(start_date_time(next_month, local_tz))})"
        )
    else:
        lookback_days = max(1, int(os.getenv("EV_TELEMETRY_LOOKBACK_DAYS", "180")))
        range_clause = f"|> range(start: -{lookback_days}d)"
    query = f"""
import "date"
import "timezone"

option location = timezone.location(name: "{local_tz.key}")

from(bucket: "{influx_bucket()}")
  {range_clause}
  |> filter(fn: (r) => r._measurement == "enphase_ev_charger_telemetry")
  |> filter(fn: (r) => r._field == "consumption_wh")
  |> map(fn: (r) => ({{ r with month: string(v: date.month(t: r._time)) }}))
  |> group(columns: ["serial_no", "month"])
  |> sum()
"""
    client = influx_client()
    try:
        monthly_rows = query_rows(client, query, org=influx_org())
    finally:
        client.close()
    for row in monthly_rows:
        print(
            {
                "serial_no": row.get("serial_no"),
                "month": row.get("month"),
                "kwh": round(float(row.get("_value") or 0.0) / 1000.0, 3),
            }
        )

    if not args.month:
        return

    start = date.fromisoformat(f"{args.month}-01")
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    daily_wh = defaultdict(float)
    intervals = 0
    for row in query_measurement_rows(
        "enphase_ev_charger_telemetry",
        start=flux_time(start_date_time(start, local_tz)),
        stop=flux_time(start_date_time(next_month, local_tz)),
        fields=["consumption_wh"],
        keep_columns=["_time", "_value", "serial_no"],
    ):
        timestamp = row["_time"].astimezone(local_tz)
        daily_wh[timestamp.date().isoformat()] += float(row.get("_value") or 0.0)
        intervals += 1
    print(
        json.dumps(
            {
                "month": args.month,
                "intervals": intervals,
                "days_present": len(daily_wh),
                "active_days": sum(1 for value in daily_wh.values() if value > 0),
                "total_kwh": round(sum(daily_wh.values()) / 1000.0, 3),
                "daily_kwh": {
                    day: round(value / 1000.0, 3)
                    for day, value in sorted(daily_wh.items())
                    if value > 0
                },
            },
            indent=2,
        )
    )


def start_date_time(value: date, local_tz: ZoneInfo):
    return datetime.combine(value, datetime.min.time(), tzinfo=local_tz)


if __name__ == "__main__":
    main()
