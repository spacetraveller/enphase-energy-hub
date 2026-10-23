import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

from cloud_common import cloud_get, compact, env_required, redact, write_json_snapshot

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import (  # noqa: E402
    flux_time,
    influx_bucket,
    influx_client,
    influx_org,
    query_rows,
)


HISTORY_MEASUREMENT = "enphase_history_15m"


def last_history_date(client: InfluxDBClient, bucket: str, local_tz: ZoneInfo) -> date:
    query = f"""
from(bucket: "{bucket}")
  |> range(start: 2024-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{HISTORY_MEASUREMENT}")
  |> filter(fn: (r) => r._field == "energy_produced_wh")
  |> group()
  |> last()
  |> keep(columns: ["_time"])
"""
    rows = query_rows(client, query, org=influx_org())
    if not rows:
        raise RuntimeError("No imported history rows found.")
    return rows[0]["_time"].astimezone(local_tz).date()


def boundary_rows(client: InfluxDBClient, bucket: str, local_day: date, local_tz: ZoneInfo) -> list[dict[str, Any]]:
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=local_tz)
    stop = start + timedelta(days=1)
    query = f"""
data = from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{HISTORY_MEASUREMENT}")
  |> filter(fn: (r) => r._field == "energy_produced_wh")
  |> keep(columns: ["_time", "_value"])
  |> group()

first = data |> first() |> set(key: "point", value: "first")
last = data |> last() |> set(key: "point", value: "last")

union(tables: [first, last])
"""
    return query_rows(client, query, org=influx_org())


def cloud_intervals(system_id: str, local_day: date, local_tz: ZoneInfo) -> dict[str, Any]:
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=local_tz)
    stop = start + timedelta(days=1)
    params = {"start_at": str(int(start.timestamp())), "end_at": str(int(stop.timestamp()))}
    response = cloud_get(f"/systems/{system_id}/telemetry/production_meter", params=params)
    payload = response.json()
    intervals = payload.get("intervals", []) if isinstance(payload, dict) else []
    return {
        "params": params,
        "status_code": response.status_code,
        "summary": compact(redact(payload)),
        "interval_count": len(intervals) if isinstance(intervals, list) else None,
        "first_interval": intervals[0] if intervals else None,
        "last_interval": intervals[-1] if intervals else None,
    }


def render_epoch(value: int, local_tz: ZoneInfo) -> dict[str, str]:
    timestamp = datetime.fromtimestamp(value, tz=timezone.utc)
    return {
        "epoch": str(value),
        "utc": timestamp.isoformat(),
        "local": timestamp.astimezone(local_tz).isoformat(),
    }


def main() -> None:
    system_id = env_required("ENPHASE_CLOUD_SYSTEM_ID")
    bucket = influx_bucket()
    local_tz = ZoneInfo(os.getenv("ENPHASE_HISTORY_TIMEZONE", "UTC"))
    client = influx_client()

    try:
        local_day = last_history_date(client, bucket, local_tz)
        local_start = datetime.combine(local_day, datetime.min.time(), tzinfo=local_tz)
        local_stop = local_start + timedelta(days=1)
        local_rows = boundary_rows(client, bucket, local_day, local_tz)
    finally:
        client.close()
    cloud = cloud_intervals(system_id, local_day, local_tz)

    print(f"history_day={local_day.isoformat()} timezone={local_tz.key}")
    print(f"local_window={local_start.isoformat()}..{local_stop.isoformat()}")
    print(f"utc_window={local_start.astimezone(timezone.utc).isoformat()}..{local_stop.astimezone(timezone.utc).isoformat()}")
    print(f"epoch_window={int(local_start.timestamp())}..{int(local_stop.timestamp())}")
    for row in local_rows:
        timestamp = row["_time"]
        print(
            f"local_history_{row['point']} "
            f"utc={timestamp.isoformat()} "
            f"local={timestamp.astimezone(local_tz).isoformat()} "
            f"value={row['_value']}"
        )
    for label in ("first_interval", "last_interval"):
        interval = cloud.get(label)
        if isinstance(interval, dict) and isinstance(interval.get("end_at"), int):
            rendered = render_epoch(interval["end_at"], local_tz)
            print(f"cloud_{label}_end utc={rendered['utc']} local={rendered['local']} payload={interval}")

    output_path, latest_path = write_json_snapshot(
        "history_time_alignment",
        {
            "system_id": system_id,
            "history_day": local_day.isoformat(),
            "timezone": local_tz.key,
            "local_window": {
                "start": local_start.isoformat(),
                "stop": local_stop.isoformat(),
                "start_utc": local_start.astimezone(timezone.utc).isoformat(),
                "stop_utc": local_stop.astimezone(timezone.utc).isoformat(),
            },
            "local_boundary_rows": [
                {
                    "point": row.get("point"),
                    "utc": row["_time"].isoformat(),
                    "local": row["_time"].astimezone(local_tz).isoformat(),
                    "value": row.get("_value"),
                }
                for row in local_rows
            ],
            "cloud": cloud,
        },
    )
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
