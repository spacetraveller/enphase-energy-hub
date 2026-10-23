import argparse
import csv
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient


HISTORY_MEASUREMENT = "enphase_history_15m"
EV_CHARGER_MEASUREMENT = "enphase_ev_charger_telemetry"
DEFAULT_TIMEZONE = "UTC"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export 15-minute historical energy data for AI analysis."
    )
    parser.add_argument("--start-date", required=True, help="Local start date, inclusive.")
    parser.add_argument("--end-date", required=True, help="Local end date, inclusive.")
    parser.add_argument(
        "--output",
        default="/app/record/history/exports/ai_worker_energy_history.csv",
        help="CSV output path.",
    )
    parser.add_argument(
        "--timezone",
        default=os.getenv("ENPHASE_HISTORY_TIMEZONE", DEFAULT_TIMEZONE),
        help="Local timezone for timestamps.",
    )
    parser.add_argument(
        "--ev-telemetry-start-date",
        default=os.getenv("EV_CHARGER_TELEMETRY_START_DATE", ""),
        help="Optional local date from which EV charger telemetry should be expected; defaults to --start-date.",
    )
    return parser.parse_args()


def flux_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_range(start_day: date, end_day: date, local_tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=local_tz)
    stop = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=local_tz)
    return start, stop


def empty_row(timestamp: datetime) -> dict[str, object]:
    return {
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "solar_production_kwh": "",
        "home_consumption_kwh": "",
        "grid_import_kwh": "",
        "grid_export_kwh": "",
        "ev_charger_kwh": "",
        "ev_charger_data_available": "false",
        "day_type": "weekend" if timestamp.weekday() >= 5 else "weekday",
        "notes": "",
    }


def query_history(client: InfluxDBClient, bucket: str, start: datetime, stop: datetime) -> dict[datetime, dict[str, object]]:
    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{HISTORY_MEASUREMENT}")
  |> filter(fn: (r) =>
    r._field == "energy_produced_wh" or
    r._field == "energy_consumed_wh" or
    r._field == "imported_from_grid_wh" or
    r._field == "exported_to_grid_wh"
  )
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "energy_produced_wh", "energy_consumed_wh", "imported_from_grid_wh", "exported_to_grid_wh"])
  |> sort(columns: ["_time"])
"""
    rows: dict[datetime, dict[str, object]] = {}
    for table in client.query_api().query(query):
        for record in table.records:
            timestamp = record.get_time()
            if timestamp is None:
                continue
            values = record.values
            rows[timestamp] = {
                "solar_production_kwh": wh_to_kwh(values.get("energy_produced_wh")),
                "home_consumption_kwh": wh_to_kwh(values.get("energy_consumed_wh")),
                "grid_import_kwh": wh_to_kwh(values.get("imported_from_grid_wh")),
                "grid_export_kwh": wh_to_kwh(values.get("exported_to_grid_wh")),
            }
    return rows


def query_ev_charger(client: InfluxDBClient, bucket: str, start: datetime, stop: datetime) -> dict[datetime, float]:
    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{EV_CHARGER_MEASUREMENT}" and r._field == "consumption_wh")
  |> group(columns: ["_time"])
  |> sum()
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
"""
    rows: dict[datetime, float] = {}
    for table in client.query_api().query(query):
        for record in table.records:
            timestamp = record.get_time()
            value = record.get_value()
            if timestamp is None or value is None:
                continue
            rows[timestamp] = round(float(value) / 1000.0, 6)
    return rows


def wh_to_kwh(value: object) -> str:
    if value is None:
        return ""
    return f"{float(value) / 1000.0:.6f}"


def iter_interval_starts(start: datetime, stop: datetime) -> list[datetime]:
    intervals = []
    current = start
    while current < stop:
        intervals.append(current)
        current += timedelta(minutes=15)
    return intervals


def write_csv(
    output_path: Path,
    intervals: list[datetime],
    history_rows: dict[datetime, dict[str, object]],
    ev_rows: dict[datetime, float],
    ev_telemetry_start_day: date,
) -> None:
    fieldnames = [
        "timestamp",
        "solar_production_kwh",
        "home_consumption_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "ev_charger_kwh",
        "ev_charger_data_available",
        "day_type",
        "notes",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for interval in intervals:
            row = empty_row(interval)
            row.update(history_rows.get(interval.astimezone(timezone.utc), {}))

            ev_value = ev_rows.get(interval.astimezone(timezone.utc))
            if interval.date() < ev_telemetry_start_day:
                row["ev_charger_kwh"] = "0.000000"
                row["ev_charger_data_available"] = "false"
            elif ev_value is not None:
                row["ev_charger_kwh"] = f"{ev_value:.6f}"
                row["ev_charger_data_available"] = "true"

            writer.writerow(row)


def main() -> None:
    args = parse_args()
    local_tz = ZoneInfo(args.timezone)
    start_day = date.fromisoformat(args.start_date)
    end_day = date.fromisoformat(args.end_date)
    ev_telemetry_start_day = date.fromisoformat(args.ev_telemetry_start_date or args.start_date)
    start, stop = local_range(start_day, end_day, local_tz)

    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )
    bucket = os.environ["INFLUXDB_BUCKET"]

    intervals = iter_interval_starts(start, stop)
    history_rows = query_history(client, bucket, start, stop)
    ev_rows = query_ev_charger(client, bucket, start, stop)
    output_path = Path(args.output)
    write_csv(output_path, intervals, history_rows, ev_rows, ev_telemetry_start_day)

    print(
        {
            "output": str(output_path),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "timezone": args.timezone,
            "intervals": len(intervals),
            "history_intervals": len(history_rows),
            "ev_charger_intervals": len(ev_rows),
            "ev_telemetry_start_date": ev_telemetry_start_day.isoformat(),
        }
    )


if __name__ == "__main__":
    main()
