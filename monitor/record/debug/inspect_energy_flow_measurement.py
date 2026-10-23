import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from influxdb_client import InfluxDBClient
from influxdb_client.rest import ApiException

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import flux_time, influx_bucket, influx_client, influx_org, query_rows  # noqa: E402


MEASUREMENT = os.getenv("TARGET_MEASUREMENT", "enphase_energy_flow")
FIELDS = [
    "w_now",
    "wh_today",
    "wh_lifetime",
    "active_count",
    "reading_time",
]


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def field_keys(client: InfluxDBClient, bucket: str, start: datetime, stop: datetime) -> list[str]:
    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> keep(columns: ["_field"])
  |> group()
  |> distinct(column: "_field")
"""
    return sorted(str(row["_value"]) for row in query_rows(client, query, org=influx_org()))


def tag_sets(
    client: InfluxDBClient,
    bucket: str,
    field: str,
    start: datetime,
    stop: datetime,
) -> list[dict[str, str]]:
    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{field}")
  |> keep(columns: ["section", "type", "measurement_type"])
  |> group()
  |> distinct(column: "section")
"""
    try:
        query_rows(client, query, org=influx_org())
    except ApiException:
        return []

    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{field}")
  |> keep(columns: ["section", "type", "measurement_type"])
  |> group(columns: ["section", "type", "measurement_type"])
  |> limit(n: 1)
"""
    rows = query_rows(client, query, org=influx_org())
    tags = []
    for row in rows:
        tags.append(
            {
                "section": str(row.get("section", "")),
                "type": str(row.get("type", "")),
                "measurement_type": str(row.get("measurement_type", "")),
            }
        )
    return tags


def sample_field(
    client: InfluxDBClient,
    bucket: str,
    field: str,
    start: datetime,
    stop: datetime,
    tags: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    tag_filter = ""
    if tags:
        for key, value in tags.items():
            if value:
                tag_filter += f' and r.{key} == "{value}"'

    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{field}"{tag_filter})
  |> keep(columns: ["_time", "_value", "section", "type", "measurement_type"])
  |> limit(n: 5)
"""
    try:
        rows = query_rows(client, query, org=influx_org())
    except ApiException as exc:
        return f"error {exc.status}: {exc.reason}", []

    samples = []
    for row in rows:
        value = row.get("_value")
        samples.append(
            {
                "time": row.get("_time"),
                "type": value_type(value),
                "value": value,
                "section": row.get("section"),
                "measurement_type": row.get("measurement_type"),
                "item_type": row.get("type"),
            }
        )
    return "ok", samples


def main() -> None:
    bucket = influx_bucket()
    days = env_int("INSPECT_DAYS", 3)
    hours_per_slice = env_int("INSPECT_HOURS_PER_SLICE", 1)
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(days=days)
    client = influx_client()

    print(f"measurement={MEASUREMENT} range={flux_time(start)}..{flux_time(stop)}")
    try:
        try:
            keys = field_keys(client, bucket, start, stop)
            print(f"field_keys={keys}")
        except ApiException as exc:
            print(f"field_keys_error status={exc.status} reason={exc.reason}")
            keys = FIELDS

        fields = [field for field in FIELDS if field in keys or not keys]
        if not fields:
            fields = FIELDS

        for field in fields:
            print(f"\nfield={field}")
            status_counts: dict[str, int] = defaultdict(int)
            type_counts: dict[str, int] = defaultdict(int)
            first_by_type: dict[str, dict[str, Any]] = {}
            first_error: tuple[datetime, str] | None = None
            cursor = start
            while cursor < stop:
                window_stop = min(cursor + timedelta(hours=hours_per_slice), stop)
                status, samples = sample_field(client, bucket, field, cursor, window_stop)
                status_counts[status] += 1
                if status != "ok" and first_error is None:
                    first_error = (cursor, status)
                for sample in samples:
                    sample_type = str(sample["type"])
                    type_counts[sample_type] += 1
                    first_by_type.setdefault(sample_type, sample)
                cursor = window_stop

            print(f"window_statuses={dict(status_counts)}")
            print(f"sampled_types={dict(type_counts)}")
            if first_error:
                print(f"first_error_window={flux_time(first_error[0])} status={first_error[1]}")
            for sample_type, sample in first_by_type.items():
                time_value = sample["time"].isoformat() if sample["time"] else None
                print(
                    "first_{sample_type}=time:{time} value:{value} section:{section} measurement_type:{measurement_type} item_type:{item_type}".format(
                        sample_type=sample_type,
                        time=time_value,
                        value=sample["value"],
                        section=sample["section"],
                        measurement_type=sample["measurement_type"],
                        item_type=sample["item_type"],
                    )
                )

            tags = tag_sets(client, bucket, field, start, stop)
            if tags:
                print("tag_samples:")
                for tag in tags[:10]:
                    status, samples = sample_field(client, bucket, field, start, stop, tag)
                    sample_bits = [
                        f"{sample['time'].isoformat()} {sample['type']} {sample['value']}"
                        for sample in samples[:2]
                    ]
                    print(f"  {tag} status={status} samples={sample_bits}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
