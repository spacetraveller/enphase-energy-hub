import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


FIELD_MAP = {
    "Energy Produced (Wh)": "energy_produced_wh",
    "Energy Consumed (Wh)": "energy_consumed_wh",
    "Exported to Grid (Wh)": "exported_to_grid_wh",
    "Imported from Grid (Wh)": "imported_from_grid_wh",
}


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")


def parse_float(value: str) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def csv_files(inbox: Path) -> list[Path]:
    return sorted(
        path
        for path in inbox.glob("*.csv")
        if path.is_file() and not path.name.startswith(".~lock")
    )


def row_key(row: dict[str, str]) -> datetime:
    return parse_timestamp(row["Date/Time"])


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader if row.get("Date/Time")]


def build_point(timestamp: datetime, row: dict[str, str], source_file: str) -> Point:
    point = (
        Point("enphase_history_15m")
        .tag("source", "enlighten_export")
        .tag("source_file", source_file)
        .field("interval_minutes", 15)
        .time(timestamp, WritePrecision.NS)
    )
    for source_field, influx_field in FIELD_MAP.items():
        point.field(influx_field, parse_float(row[source_field]))
    return point


def main() -> None:
    inbox = Path(os.getenv("HISTORY_INBOX", "/app/record/inbox"))
    batch_size = int(os.getenv("HISTORY_IMPORT_BATCH_SIZE", "500"))
    bucket = os.environ["INFLUXDB_BUCKET"]
    org = os.environ["INFLUXDB_ORG"]
    seen: dict[datetime, tuple[dict[str, str], str]] = {}

    files = csv_files(inbox)
    if not files:
        raise RuntimeError(f"No CSV files found in {inbox}")

    for path in files:
        for row in read_rows(path):
            seen[row_key(row)] = (row, path.name)

    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=org,
    )
    write_api = client.write_api(write_options=SYNCHRONOUS)

    points: list[Point] = []
    written = 0
    for timestamp in sorted(seen):
        row, source_file = seen[timestamp]
        points.append(build_point(timestamp, row, source_file))
        if len(points) >= batch_size:
            write_api.write(bucket=bucket, org=org, record=points)
            written += len(points)
            points = []

    if points:
        write_api.write(bucket=bucket, org=org, record=points)
        written += len(points)

    first = min(seen).isoformat()
    last = max(seen).isoformat()
    print(
        {
            "files": len(files),
            "unique_timestamps": len(seen),
            "points_written": written,
            "first": first,
            "last": last,
            "measurement": "enphase_history_15m",
        }
    )


if __name__ == "__main__":
    main()
