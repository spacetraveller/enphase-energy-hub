import argparse
import os
import statistics
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor import ENERGY_FLOW_MEASUREMENT
from record.query_helpers import env_timezone
from record.query_helpers import percentile_linear as shared_percentile_linear
from record.query_helpers import parse_hhmm as shared_parse_hhmm


LOCAL_TZ = ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
UTC_TZ = ZoneInfo("UTC")


def percentile(values: list[float], pct: float) -> float:
    if not values:
        raise ValueError("no values")
    return shared_percentile_linear(values, pct)


def parse_hhmm(value: str) -> time:
    # Keep strptime's strict HH:MM acceptance and error behavior, then share conversion.
    datetime.strptime(value, "%H:%M")
    return shared_parse_hhmm(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize overnight household baseload variation from energy-flow total consumption."
    )
    parser.add_argument(
        "--date",
        default=datetime.now(LOCAL_TZ).date().isoformat(),
        help="Local date to query, default: today.",
    )
    parser.add_argument("--start", default="00:00", help="Local start time, HH:MM.")
    parser.add_argument("--end", default="05:00", help="Local end time, HH:MM.")
    args = parser.parse_args()

    local_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    start_local = datetime.combine(local_date, parse_hhmm(args.start), tzinfo=LOCAL_TZ)
    end_local = datetime.combine(local_date, parse_hhmm(args.end), tzinfo=LOCAL_TZ)
    if end_local <= start_local:
        raise SystemExit("--end must be after --start on the same local date")

    start_utc = start_local.astimezone(UTC_TZ)
    end_utc = end_local.astimezone(UTC_TZ)

    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )
    query = f"""
from(bucket: "enphase")
  |> range(start: {start_utc.isoformat()}, stop: {end_utc.isoformat()})
  |> filter(fn: (r) => r._measurement == "{ENERGY_FLOW_MEASUREMENT}")
  |> filter(fn: (r) => r.section == "consumption")
  |> filter(fn: (r) => r.measurement_type == "total-consumption")
  |> filter(fn: (r) => r._field == "w_now")
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
"""
    values: list[tuple[str, float]] = []
    for table in client.query_api().query(query):
        for record in table.records:
            values.append((record.get_time().astimezone(LOCAL_TZ).isoformat(), float(record.get_value())))

    watts = [value for _, value in values]
    if not watts:
        print(
            {
                "date": args.date,
                "window_local": f"{args.start}-{args.end}",
                "points": 0,
                "message": "No total-consumption w_now points found.",
            }
        )
        return

    deltas = [abs(current - previous) for previous, current in zip(watts, watts[1:])]
    high_threshold = percentile(watts, 0.95)
    high_runs: list[list[tuple[str, float]]] = []
    current_run: list[tuple[str, float]] = []
    for item in values:
        if item[1] >= high_threshold:
            current_run.append(item)
        elif current_run:
            high_runs.append(current_run)
            current_run = []
    if current_run:
        high_runs.append(current_run)

    high_run_summary = [
        {
            "start": run[0][0],
            "end": run[-1][0],
            "points": len(run),
            "min_w": round(min(value for _, value in run), 3),
            "max_w": round(max(value for _, value in run), 3),
        }
        for run in high_runs
    ]

    summary = {
        "date": args.date,
        "timezone": str(LOCAL_TZ),
        "window_local": f"{args.start}-{args.end}",
        "first_point": values[0][0],
        "last_point": values[-1][0],
        "points": len(watts),
        "watts": {
            "min": round(min(watts), 3),
            "p05": round(percentile(watts, 0.05), 3),
            "p10": round(percentile(watts, 0.10), 3),
            "median": round(statistics.median(watts), 3),
            "mean": round(statistics.mean(watts), 3),
            "p90": round(percentile(watts, 0.90), 3),
            "p95": round(percentile(watts, 0.95), 3),
            "max": round(max(watts), 3),
            "range": round(max(watts) - min(watts), 3),
            "p90_p10_spread": round(percentile(watts, 0.90) - percentile(watts, 0.10), 3),
            "p95_p05_spread": round(percentile(watts, 0.95) - percentile(watts, 0.05), 3),
            "stddev": round(statistics.pstdev(watts), 3),
        },
        "cycle_to_cycle_abs_delta_watts": {
            "median": round(statistics.median(deltas), 3) if deltas else 0.0,
            "p90": round(percentile(deltas, 0.90), 3) if deltas else 0.0,
            "max": round(max(deltas), 3) if deltas else 0.0,
        },
        "high_readings": {
            "threshold_w": round(high_threshold, 3),
            "points_at_or_above_threshold": sum(1 for value in watts if value >= high_threshold),
            "runs": len(high_runs),
            "longest_run_points": max((len(run) for run in high_runs), default=0),
            "runs_detail": high_run_summary,
        },
    }
    print(summary)


if __name__ == "__main__":
    main()
