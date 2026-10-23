import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime
from statistics import mean
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient


LOCAL_TZ = ZoneInfo(os.getenv("HISTORY_TIMEZONE", "UTC"))
DEFAULT_START = os.getenv("HISTORY_QUERY_START", "2024-10-01T00:00:00Z")
MEASUREMENT = "enphase_history_15m"
FIELDS = {
    "solar": "energy_produced_wh",
    "consumption": "energy_consumed_wh",
    "export": "exported_to_grid_wh",
    "import": "imported_from_grid_wh",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query daily history totals and find days matching a threshold."
    )
    parser.add_argument(
        "--metric",
        choices=sorted(FIELDS),
        default=os.getenv("HISTORY_QUERY_METRIC", "solar"),
    )
    parser.add_argument(
        "--max-kwh",
        type=float,
        default=float(os.getenv("HISTORY_QUERY_MAX_KWH", "2.8")),
        help="Match days with a daily total less than or equal to this value.",
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START,
        help="InfluxDB range start, for example 2024-10-01T00:00:00Z.",
    )
    parser.add_argument(
        "--stop-date",
        default=os.getenv("HISTORY_QUERY_STOP_DATE"),
        help="Optional exclusive local stop date, for example 2026-05-18.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.getenv("HISTORY_QUERY_LIMIT", "20")),
        help="Number of matching days to include in the output.",
    )
    parser.add_argument(
        "--month",
        type=int,
        choices=range(1, 13),
        default=None,
        help="Optional local calendar month filter, from 1 to 12.",
    )
    return parser.parse_args()


def query_rows(client: InfluxDBClient, query: str, org: str) -> list[dict]:
    rows = []
    for table in client.query_api().query(query, org=org):
        for record in table.records:
            rows.append(record.values)
    return rows


def stop_clause(stop_date: str | None) -> str:
    if not stop_date:
        return ""
    parsed = date.fromisoformat(stop_date)
    return f", stop: {parsed.isoformat()}T00:00:00+10:00"


def main() -> None:
    args = parse_args()
    bucket = os.environ["INFLUXDB_BUCKET"]
    org = os.environ["INFLUXDB_ORG"]
    field = FIELDS[args.metric]

    query = f'''
from(bucket: "{bucket}")
  |> range(start: {args.start}{stop_clause(args.stop_date)})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{field}")
  |> keep(columns: ["_time", "_value", "source"])
'''
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=org,
    )
    rows = query_rows(client, query, org)
    client.close()

    by_time: dict[datetime, tuple[int, float]] = {}
    for row in rows:
        local_time = row["_time"].astimezone(LOCAL_TZ)
        priority = 1 if row.get("source") == "enlighten_export" else 0
        existing = by_time.get(local_time)
        if existing is None or priority >= existing[0]:
            by_time[local_time] = (priority, float(row.get("_value") or 0.0))

    by_day: dict[str, float] = defaultdict(float)
    for local_time, (_, value) in by_time.items():
        by_day[local_time.date().isoformat()] += value / 1000.0

    daily = sorted(
        ({"date": day, "kwh": round(kwh, 3)} for day, kwh in by_day.items()),
        key=lambda row: row["date"],
    )
    if args.month:
        daily = [
            row
            for row in daily
            if date.fromisoformat(row["date"]).month == args.month
        ]
    matches = [row for row in daily if row["kwh"] <= args.max_kwh]
    top_days = sorted(daily, key=lambda row: row["kwh"], reverse=True)[: args.limit]
    by_year: dict[int, list[float]] = defaultdict(list)
    for row in daily:
        by_year[date.fromisoformat(row["date"]).year].append(row["kwh"])

    print(
        json.dumps(
            {
                "timezone": str(LOCAL_TZ),
                "measurement": MEASUREMENT,
                "metric": args.metric,
                "field": field,
                "max_kwh": args.max_kwh,
                "start": args.start,
                "exclusive_stop_date": args.stop_date,
                "month": args.month,
                "history_days": len(daily),
                "first_recorded_day": daily[0]["date"] if daily else None,
                "last_recorded_day": daily[-1]["date"] if daily else None,
                "matching_day_count": len(matches),
                "last_matching_day": matches[-1] if matches else None,
                "recent_matching_days": list(reversed(matches[-args.limit :])),
                "lowest_days": sorted(daily, key=lambda row: row["kwh"])[: args.limit],
                "top_days": top_days,
                "year_summary": {
                    str(year): {
                        "days": len(values),
                        "mean_kwh": round(mean(values), 3),
                        "max_kwh": round(max(values), 3),
                    }
                    for year, values in sorted(by_year.items())
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
