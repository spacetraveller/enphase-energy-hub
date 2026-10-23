import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from cloud_common import cloud_get, env_int, env_required, local_history_timezone, require_cloud_success, write_json_snapshot


HISTORY_MEASUREMENT = "enphase_history_15m"
INTERVAL_SECONDS = 15 * 60
FIELDS = {
    "energy_produced_wh": {
        "endpoint": "telemetry/production_meter",
        "interval_fields": ("wh_del",),
    },
    "energy_consumed_wh": {
        "endpoint": "telemetry/consumption_meter",
        "interval_fields": ("enwh",),
    },
    "exported_to_grid_wh": {
        "endpoint": "energy_export_telemetry",
        "interval_fields": ("wh_exported",),
    },
    "imported_from_grid_wh": {
        "endpoint": "energy_import_telemetry",
        "interval_fields": ("wh_imported",),
    },
}


def flux_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def query_rows(client: InfluxDBClient, query: str) -> list[dict[str, Any]]:
    rows = []
    for table in client.query_api().query(query):
        for record in table.records:
            rows.append(record.values)
    return rows


def last_history_date(client: InfluxDBClient, bucket: str, local_tz: ZoneInfo) -> date | None:
    query = f"""
from(bucket: "{bucket}")
  |> range(start: 2024-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{HISTORY_MEASUREMENT}")
  |> filter(fn: (r) => r._field == "energy_produced_wh")
  |> group()
  |> last()
  |> keep(columns: ["_time"])
"""
    rows = query_rows(client, query)
    if not rows:
        return None
    return rows[0]["_time"].astimezone(local_tz).date()


def existing_point_count(
    client: InfluxDBClient,
    bucket: str,
    start_day: date,
    stop_day: date,
    local_tz: ZoneInfo,
) -> int:
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=local_tz)
    stop = datetime.combine(stop_day, datetime.min.time(), tzinfo=local_tz)
    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{HISTORY_MEASUREMENT}")
  |> filter(fn: (r) => r._field == "energy_produced_wh")
  |> group()
  |> count()
"""
    rows = query_rows(client, query)
    if not rows:
        return 0
    return int(rows[0]["_value"])


def flatten_intervals(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, list):
            result.extend(child for child in item if isinstance(child, dict))
    return result


def interval_value(interval: dict[str, Any], field_names: tuple[str, ...]) -> float | None:
    for field_name in field_names:
        value = interval.get(field_name)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def fetch_field_intervals(
    system_id: str,
    history_field: str,
    config: dict[str, Any],
    start_day: date,
    stop_day: date,
    local_tz: ZoneInfo,
    granularity: str,
) -> tuple[list[tuple[datetime, float]], dict[str, Any]]:
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=local_tz)
    stop = datetime.combine(stop_day, datetime.min.time(), tzinfo=local_tz)
    params = {"start_at": str(int(start.timestamp()))}
    if granularity:
        params["granularity"] = granularity
    else:
        params["end_at"] = str(int(stop.timestamp()))
    path = f"/systems/{system_id}/{config['endpoint']}"
    response = cloud_get(path, params=params)
    require_cloud_success(response, "history_cloud_backfill")
    payload = response.json()
    intervals = flatten_intervals(payload.get("intervals") if isinstance(payload, dict) else None)

    values: list[tuple[datetime, float]] = []
    for interval in intervals:
        end_at = interval.get("end_at")
        if not isinstance(end_at, int):
            continue
        value = interval_value(interval, config["interval_fields"])
        if value is None:
            continue
        timestamp = datetime.fromtimestamp(end_at - INTERVAL_SECONDS, tz=timezone.utc)
        local_date = timestamp.astimezone(local_tz).date()
        if start_day <= local_date < stop_day:
            values.append((timestamp, value))

    metadata = {
        "history_field": history_field,
        "path": path,
        "params": params,
        "status_code": response.status_code,
        "intervals_returned": len(intervals),
        "values_extracted": len(values),
        "expected_start_day": start_day.isoformat(),
        "expected_stop_day_exclusive": stop_day.isoformat(),
        "ok": 200 <= response.status_code < 300,
    }
    return values, metadata


def chunk_ranges(start_day: date, end_day: date, chunk_days: int) -> list[tuple[date, date]]:
    chunks = []
    current = start_day
    while current <= end_day:
        chunk_stop = min(current + timedelta(days=chunk_days), end_day + timedelta(days=1))
        chunks.append((current, chunk_stop))
        current = chunk_stop
    return chunks


def build_points(field_values: dict[str, list[tuple[datetime, float]]]) -> list[Point]:
    by_time: dict[datetime, dict[str, float]] = {}
    for field, values in field_values.items():
        for timestamp, value in values:
            by_time.setdefault(timestamp, {})[field] = value

    points: list[Point] = []
    for timestamp in sorted(by_time):
        fields = by_time[timestamp]
        if not fields:
            continue
        point = (
            Point(HISTORY_MEASUREMENT)
            .tag("source", "enlighten_cloud_api")
            .tag("source_file", "api_v4_telemetry")
            .field("interval_minutes", 15)
            .time(timestamp, WritePrecision.NS)
        )
        for field in FIELDS:
            point.field(field, float(fields.get(field, 0.0)))
        points.append(point)
    return points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill enphase_history_15m from Enphase API v4 telemetry after the last imported day."
    )
    parser.add_argument("--execute", action="store_true", help="Write points to InfluxDB.")
    parser.add_argument("--start-date", default="", help="Local start date, inclusive. Defaults to day after latest history.")
    parser.add_argument("--end-date", default="", help="Local end date, inclusive. Defaults to yesterday.")
    parser.add_argument(
        "--granularity",
        default=os.getenv("ENPHASE_CLOUD_BACKFILL_GRANULARITY", "day"),
        help="Cloud telemetry granularity string. Use day for 96 x 15-minute intervals; 15mins returns one interval.",
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=env_int("ENPHASE_CLOUD_BACKFILL_CHUNK_DAYS", 1),
        help="Number of local days per cloud request chunk. Use 1 with granularity=day.",
    )
    parser.add_argument("--allow-overlap", action="store_true", help="Allow writing when history points already exist in the range.")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    system_id = env_required("ENPHASE_CLOUD_SYSTEM_ID")
    bucket = os.environ["INFLUXDB_BUCKET"]
    org = os.environ["INFLUXDB_ORG"]
    local_tz = local_history_timezone()
    now = datetime.now(local_tz)
    yesterday = now.date() - timedelta(days=1)
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=org,
    )

    latest_day = last_history_date(client, bucket, local_tz)
    if args.start_date:
        start_day = date.fromisoformat(args.start_date)
    elif latest_day:
        start_day = latest_day + timedelta(days=1)
    else:
        raise RuntimeError("No existing history found. Provide --start-date for an initial cloud import.")

    end_day = date.fromisoformat(args.end_date) if args.end_date else yesterday
    stop_day = end_day + timedelta(days=1)
    if start_day > end_day:
        print(f"backfill_needed=false latest_history_day={latest_day} yesterday={yesterday}")
        return

    existing = existing_point_count(client, bucket, start_day, stop_day, local_tz)
    if existing and not args.allow_overlap:
        raise RuntimeError(
            f"Refusing to write because {existing} existing history intervals were found "
            f"from {start_day} to {end_day}. Use --allow-overlap only after checking dashboard impact."
        )

    delay_seconds = max(0, env_int("ENPHASE_CLOUD_REQUEST_DELAY_SECONDS", 7))
    endpoint_results = []
    request_index = 0
    points_built = 0
    written = 0
    write_api = client.write_api(write_options=SYNCHRONOUS) if args.execute else None
    for chunk_start, chunk_stop in chunk_ranges(start_day, end_day, args.chunk_days):
        chunk_field_values: dict[str, list[tuple[datetime, float]]] = {field: [] for field in FIELDS}
        for field, config in FIELDS.items():
            if request_index:
                time.sleep(delay_seconds)
            values, metadata = fetch_field_intervals(
                system_id,
                field,
                config,
                chunk_start,
                chunk_stop,
                local_tz,
                args.granularity,
            )
            metadata["chunk_start_day"] = chunk_start.isoformat()
            metadata["chunk_stop_day_exclusive"] = chunk_stop.isoformat()
            chunk_field_values[field].extend(values)
            endpoint_results.append(metadata)
            request_index += 1
            print(
                f"{chunk_start.isoformat()}..{(chunk_stop - timedelta(days=1)).isoformat()} {field}: "
                f"status={metadata['status_code']} "
                f"intervals_returned={metadata['intervals_returned']} values={metadata['values_extracted']}"
            )

        chunk_points = build_points(chunk_field_values)
        points_built += len(chunk_points)
        chunk_written = 0
        if write_api is not None:
            for offset in range(0, len(chunk_points), args.batch_size):
                batch = chunk_points[offset : offset + args.batch_size]
                write_api.write(bucket=bucket, org=org, record=batch)
                chunk_written += len(batch)
            written += chunk_written
        print(
            f"chunk_complete={chunk_start.isoformat()}..{(chunk_stop - timedelta(days=1)).isoformat()} "
            f"points_built={len(chunk_points)} points_written={chunk_written}"
        )

    expected_intervals = ((end_day - start_day).days + 1) * 96
    print(
        f"range={start_day.isoformat()}..{end_day.isoformat()} "
        f"expected_intervals={expected_intervals} points_built={points_built} execute={args.execute}"
    )

    if args.execute:
        print(f"points_written={written}")
    else:
        print("dry_run=true")

    output_path, latest_path = write_json_snapshot(
        "history_cloud_backfill",
        {
            "system_id": system_id,
            "timezone": local_tz.key,
            "latest_history_day_before_backfill": latest_day.isoformat() if latest_day else None,
            "start_day": start_day.isoformat(),
            "end_day": end_day.isoformat(),
            "expected_intervals": expected_intervals,
            "points_built": points_built,
            "points_written": written,
            "execute": args.execute,
            "granularity": args.granularity,
            "chunk_days": args.chunk_days,
            "requests_made": request_index,
            "existing_intervals_in_range": existing,
            "endpoint_results": endpoint_results,
        },
    )
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
