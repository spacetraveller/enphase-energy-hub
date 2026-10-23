import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from cloud_common import cloud_get, env_int, env_required, require_cloud_success, write_json_snapshot


MEASUREMENT = "enphase_ev_charger_telemetry"
INTERVAL_SECONDS = 15 * 60


def date_range(start_day: date, end_day: date) -> list[date]:
    days = []
    current = start_day
    while current <= end_day:
        days.append(current)
        current += timedelta(days=1)
    return days


def fetch_day(system_id: str, serial: str, local_day: date, local_tz: ZoneInfo) -> tuple[list[Point], dict[str, Any]]:
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=local_tz)
    path = f"/systems/{system_id}/ev_charger/{serial}/telemetry"
    params = {
        "start_at": str(int(start.timestamp())),
        "granularity": "day",
    }
    response = cloud_get(path, params=params)
    require_cloud_success(response, "ev_charger_telemetry_import")
    payload = response.json()
    consumption = payload.get("consumption") if isinstance(payload, dict) else None
    intervals = consumption if isinstance(consumption, list) else []

    points: list[Point] = []
    total_wh = 0.0
    for interval in intervals:
        if not isinstance(interval, dict):
            continue
        end_at = interval.get("end_at")
        value = interval.get("consumption")
        if not isinstance(end_at, int) or not isinstance(value, (int, float)):
            continue
        timestamp = datetime.fromtimestamp(end_at - INTERVAL_SECONDS, tz=timezone.utc)
        if timestamp.astimezone(local_tz).date() != local_day:
            continue
        total_wh += float(value)
        points.append(
            Point(MEASUREMENT)
            .tag("serial_no", serial)
            .tag("source", "enlighten_cloud_api")
            .field("consumption_wh", float(value))
            .field("interval_minutes", 15)
            .time(timestamp, WritePrecision.NS)
        )

    metadata = {
        "day": local_day.isoformat(),
        "path": path,
        "params": params,
        "status_code": response.status_code,
        "intervals_returned": len(intervals),
        "points_built": len(points),
        "consumption_wh": total_wh,
        "ok": 200 <= response.status_code < 300,
    }
    return points, metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import EV charger telemetry into InfluxDB.")
    parser.add_argument("--start-date", required=True, help="Local start date, inclusive.")
    parser.add_argument("--end-date", required=True, help="Local end date, inclusive.")
    parser.add_argument("--execute", action="store_true", help="Write points to InfluxDB.")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    system_id = env_required("ENPHASE_CLOUD_SYSTEM_ID")
    serials = [serial.strip() for serial in env_required("ENPHASE_CLOUD_EVSE_SERIALS").split(",") if serial.strip()]
    serial = os.getenv("ENPHASE_CLOUD_EVSE_SERIAL", serials[0])
    local_tz = ZoneInfo(os.getenv("ENPHASE_HISTORY_TIMEZONE", "UTC"))
    start_day = date.fromisoformat(args.start_date)
    end_day = date.fromisoformat(args.end_date)
    delay_seconds = max(0, env_int("ENPHASE_CLOUD_REQUEST_DELAY_SECONDS", 7))

    day_results = []
    points_built = 0
    written = 0
    client = None
    write_api = None
    if args.execute:
        client = InfluxDBClient(
            url=os.environ["INFLUXDB_URL"],
            token=os.environ["INFLUXDB_TOKEN"],
            org=os.environ["INFLUXDB_ORG"],
        )
        write_api = client.write_api(write_options=SYNCHRONOUS)
    bucket = os.environ["INFLUXDB_BUCKET"]
    org = os.environ["INFLUXDB_ORG"]
    for index, day in enumerate(date_range(start_day, end_day)):
        if index:
            time.sleep(delay_seconds)
        points, metadata = fetch_day(system_id, serial, day, local_tz)
        points_built += len(points)
        day_written = 0
        if write_api is not None:
            for offset in range(0, len(points), args.batch_size):
                batch = points[offset : offset + args.batch_size]
                write_api.write(bucket=bucket, org=org, record=batch)
                day_written += len(batch)
            written += day_written
        metadata["points_written"] = day_written
        day_results.append(metadata)
        print(
            f"{day.isoformat()}: status={metadata['status_code']} "
            f"intervals={metadata['intervals_returned']} points={metadata['points_built']} "
            f"points_written={day_written} consumption_kwh={metadata['consumption_wh'] / 1000.0:.3f}"
        )

    if client is not None:
        client.close()

    print(
        f"range={start_day.isoformat()}..{end_day.isoformat()} "
        f"points_built={points_built} points_written={written} execute={args.execute}"
    )
    output_path, latest_path = write_json_snapshot(
        "ev_charger_telemetry_import",
        {
            "system_id": system_id,
            "serial_no": serial,
            "measurement": MEASUREMENT,
            "start_day": start_day.isoformat(),
            "end_day": end_day.isoformat(),
            "points_built": points_built,
            "points_written": written,
            "execute": args.execute,
            "day_results": day_results,
        },
    )
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
