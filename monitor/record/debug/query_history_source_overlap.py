import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
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


LOCAL_TZ = ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
MEASUREMENT = "enphase_history_15m"
FIELDS = {
    "solar": "energy_produced_wh",
    "consumption": "energy_consumed_wh",
    "export": "exported_to_grid_wh",
    "import": "imported_from_grid_wh",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find overlapping imported history series caused by differing source-file tags."
    )
    parser.add_argument("--metric", choices=sorted(FIELDS), default="solar")
    parser.add_argument("--start", required=True, help="Inclusive local ISO date, for example 2026-05-01.")
    parser.add_argument("--stop", required=True, help="Exclusive local ISO date, for example 2026-06-01.")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=LOCAL_TZ)
    stop = datetime.fromisoformat(args.stop).replace(tzinfo=LOCAL_TZ)
    field = FIELDS[args.metric]
    query = f'''
from(bucket: "{influx_bucket()}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{field}")
  |> keep(columns: ["_time", "_value", "source", "source_file"])
'''

    client = influx_client()
    by_time: dict[datetime, list[dict]] = defaultdict(list)
    source_counts = Counter()
    source_kwh = Counter()
    try:
        rows = query_rows(client, query, org=influx_org())
    finally:
        client.close()
    for row in rows:
        source_file = str(row.get("source_file") or "")
        value = float(row.get("_value") or 0.0)
        timestamp = row["_time"].astimezone(LOCAL_TZ)
        by_time[timestamp].append({"source_file": source_file, "kwh": value / 1000.0})
        source_counts[source_file] += 1
        source_kwh[source_file] += value / 1000.0

    overlaps = {timestamp: rows for timestamp, rows in by_time.items() if len(rows) > 1}
    print(
        json.dumps(
            {
                "timezone": str(LOCAL_TZ),
                "measurement": MEASUREMENT,
                "metric": args.metric,
                "field": field,
                "start": start.isoformat(),
                "stop": stop.isoformat(),
                "timestamps": len(by_time),
                "rows": sum(len(rows) for rows in by_time.values()),
                "overlapping_timestamps": len(overlaps),
                "source_files": {
                    source_file: {
                        "rows": source_counts[source_file],
                        "kwh": round(source_kwh[source_file], 3),
                    }
                    for source_file in sorted(source_counts)
                },
                "first_overlap": (
                    {
                        "time": min(overlaps).isoformat(),
                        "rows": overlaps[min(overlaps)],
                    }
                    if overlaps
                    else None
                ),
                "last_overlap": (
                    {
                        "time": max(overlaps).isoformat(),
                        "rows": overlaps[max(overlaps)],
                    }
                    if overlaps
                    else None
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
