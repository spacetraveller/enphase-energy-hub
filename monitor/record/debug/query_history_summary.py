import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import (  # noqa: E402
    env_timezone,
    flux_time,
    influx_bucket,
    influx_client,
    influx_org,
    query_rows,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sum imported history energy over local calendar days.")
    parser.add_argument("--start", type=date.fromisoformat, help="Inclusive local date (YYYY-MM-DD).")
    parser.add_argument("--stop", type=date.fromisoformat, help="Exclusive local date (YYYY-MM-DD).")
    parser.add_argument("--days", type=int, default=30, help="Days before --stop when --start is omitted (default: 30).")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")

    local_tz = ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
    stop_day = args.stop or datetime.now(local_tz).date()
    start_day = args.start or stop_day - timedelta(days=args.days)
    if start_day >= stop_day:
        parser.error("--start must be before --stop")
    start = datetime.combine(start_day, time.min, tzinfo=local_tz)
    stop = datetime.combine(stop_day, time.min, tzinfo=local_tz)

    query = f"""
from(bucket: "{influx_bucket()}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "enphase_history_15m")
  |> filter(fn: (r) => r._field == "energy_produced_wh" or r._field == "energy_consumed_wh" or r._field == "exported_to_grid_wh" or r._field == "imported_from_grid_wh")
  |> group(columns: ["_field"])
  |> sum()
"""
    client = influx_client()
    try:
        rows = query_rows(client, query, org=influx_org())
    finally:
        client.close()
    for row in rows:
        print(
            {
                "field": row["_field"],
                "kwh": round(float(row["_value"]) / 1000.0, 2),
            }
        )


if __name__ == "__main__":
    main()
