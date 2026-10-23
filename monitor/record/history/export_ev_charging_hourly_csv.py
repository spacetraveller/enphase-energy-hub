import argparse
import csv
import os
from collections import defaultdict
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.query_helpers import flux_time, query_history_records


EV_MEASUREMENT = "enphase_ev_charger_telemetry"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_OUTPUT = "/app/record/history/exports/ev_charging_hourly.csv"
FIELDS = (
    "energy_produced_wh",
    "energy_consumed_wh",
    "imported_from_grid_wh",
    "exported_to_grid_wh",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export hourly EV charging and concurrent site energy data for battery analysis."
    )
    parser.add_argument("--start-date", help="Optional local start date, inclusive.")
    parser.add_argument("--end-date", help="Optional local end date, inclusive.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="CSV output path.")
    parser.add_argument(
        "--timezone",
        default=os.getenv("ENPHASE_HISTORY_TIMEZONE", DEFAULT_TIMEZONE),
        help="Local timezone for timestamps.",
    )
    parser.add_argument(
        "--grid-import-threshold-kwh",
        type=float,
        default=0.01,
        help="Hourly site-import threshold above which charging is classified as grid-assisted.",
    )
    return parser.parse_args()


def query_rows(client: InfluxDBClient, query: str) -> list[dict]:
    rows = []
    for table in client.query_api().query(query):
        for record in table.records:
            rows.append(record.values)
    return rows


def telemetry_range(client: InfluxDBClient, bucket: str) -> tuple[datetime, datetime]:
    query = f'''
from(bucket: "{bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{EV_MEASUREMENT}" and r._field == "consumption_wh")
  |> keep(columns: ["_time", "_value"])
'''
    timestamps = [row["_time"] for row in query_rows(client, query) if row.get("_time")]
    if not timestamps:
        raise RuntimeError(f"No {EV_MEASUREMENT} consumption_wh points found")
    return min(timestamps), max(timestamps)


def local_range(start_day: date, end_day: date, local_tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=local_tz)
    stop = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=local_tz)
    return start, stop


def history_intervals(
    start: datetime, stop: datetime
) -> dict[datetime, dict[str, float]]:
    intervals: dict[datetime, dict[str, float]] = defaultdict(dict)
    records = query_history_records(start=flux_time(start), stop=flux_time(stop))
    for timestamp, field, value_wh in records:
        intervals[timestamp][field] = value_wh / 1000.0
    return intervals


def ev_intervals(
    client: InfluxDBClient, bucket: str, start: datetime, stop: datetime
) -> dict[datetime, float]:
    query = f'''
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{EV_MEASUREMENT}" and r._field == "consumption_wh")
  |> keep(columns: ["_time", "_value"])
  |> group(columns: ["_time"])
  |> sum()
'''
    return {
        row["_time"]: float(row.get("_value") or 0.0) / 1000.0
        for row in query_rows(client, query)
    }


def hours(start: datetime, stop: datetime) -> list[datetime]:
    result = []
    current = start
    while current < stop:
        result.append(current)
        current += timedelta(hours=1)
    return result


def hour_intervals(hour: datetime) -> list[datetime]:
    return [
        (hour + timedelta(minutes=offset)).astimezone(timezone.utc)
        for offset in (0, 15, 30, 45)
    ]


def total(values: list[float | None]) -> float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def display(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def classify_mode(ev_kwh: float | None, site_import_kwh: float | None, threshold: float) -> str:
    if ev_kwh is None or site_import_kwh is None:
        return "insufficient_data"
    if ev_kwh <= 0.001:
        return "no_charging"
    if site_import_kwh <= threshold:
        return "green_charging"
    return "grid_assisted_charging"


def build_rows(
    start: datetime,
    stop: datetime,
    history: dict[datetime, dict[str, float]],
    ev: dict[datetime, float],
    grid_import_threshold_kwh: float,
) -> list[dict[str, object]]:
    rows = []
    for hour in hours(start, stop):
        intervals = hour_intervals(hour)
        site_values = {
            field: total([history.get(timestamp, {}).get(field) for timestamp in intervals])
            for field in FIELDS
        }
        ev_values = [ev.get(timestamp) for timestamp in intervals]
        ev_kwh = total(ev_values)
        site_import_kwh = site_values["imported_from_grid_wh"]
        conservative_ev_grid_kwh = (
            min(ev_kwh, site_import_kwh)
            if ev_kwh is not None and site_import_kwh is not None
            else None
        )
        conservative_ev_solar_kwh = (
            ev_kwh - conservative_ev_grid_kwh
            if ev_kwh is not None and conservative_ev_grid_kwh is not None
            else None
        )
        rows.append(
            {
                "hour_start_local": hour.isoformat(),
                "ev_charger_kwh": display(ev_kwh),
                "ev_telemetry_intervals_present": sum(value is not None for value in ev_values),
                "ev_telemetry_complete": str(all(value is not None for value in ev_values)).lower(),
                "inferred_charge_mode": classify_mode(
                    ev_kwh, site_import_kwh, grid_import_threshold_kwh
                ),
                "site_grid_import_kwh": display(site_import_kwh),
                "site_grid_export_kwh": display(site_values["exported_to_grid_wh"]),
                "site_solar_production_kwh": display(site_values["energy_produced_wh"]),
                "site_home_consumption_kwh": display(site_values["energy_consumed_wh"]),
                "estimated_ev_grid_kwh_conservative": display(conservative_ev_grid_kwh),
                "estimated_ev_solar_kwh_conservative": display(conservative_ev_solar_kwh),
            }
        )
    return rows


def write_csv(output_path: Path, rows: list[dict[str, object]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    local_tz = ZoneInfo(args.timezone)
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )
    bucket = os.environ["INFLUXDB_BUCKET"]
    first_telemetry, last_telemetry = telemetry_range(client, bucket)
    start_day = (
        date.fromisoformat(args.start_date)
        if args.start_date
        else first_telemetry.astimezone(local_tz).date()
    )
    end_day = (
        date.fromisoformat(args.end_date)
        if args.end_date
        else last_telemetry.astimezone(local_tz).date()
    )
    start, stop = local_range(start_day, end_day, local_tz)
    history = history_intervals(start, stop)
    ev = ev_intervals(client, bucket, start, stop)
    client.close()

    rows = build_rows(start, stop, history, ev, args.grid_import_threshold_kwh)
    output_path = Path(args.output)
    write_csv(output_path, rows)

    charging_rows = [row for row in rows if float(row["ev_charger_kwh"] or 0.0) > 0.001]
    conservative_grid_kwh = sum(
        float(row["estimated_ev_grid_kwh_conservative"] or 0.0) for row in charging_rows
    )
    print(
        {
            "output": str(output_path),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "hours": len(rows),
            "hours_with_complete_ev_telemetry": sum(
                row["ev_telemetry_complete"] == "true" for row in rows
            ),
            "charging_hours": len(charging_rows),
            "ev_charger_kwh": round(
                sum(float(row["ev_charger_kwh"] or 0.0) for row in charging_rows), 3
            ),
            "conservative_ev_grid_kwh": round(conservative_grid_kwh, 3),
            "notes": [
                "EV charger energy is measured directly.",
                "inferred_charge_mode is not the IQ2 app setting. It is inferred from concurrent whole-site grid import.",
                "estimated_ev_grid_kwh_conservative allocates concurrent whole-site grid import to the EV first.",
            ],
        }
    )


if __name__ == "__main__":
    main()
