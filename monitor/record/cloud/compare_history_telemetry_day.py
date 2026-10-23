import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

from cloud_common import cloud_get, compact, env_required, redact, write_json_snapshot


HISTORY_MEASUREMENT = "enphase_history_15m"
HISTORY_FIELDS = {
    "energy_produced_wh": {
        "label": "produced",
        "endpoint": "telemetry/production_meter",
        "interval_fields": ("wh_del", "production_wh", "energy_wh"),
    },
    "energy_consumed_wh": {
        "label": "consumed",
        "endpoint": "telemetry/consumption_meter",
        "interval_fields": ("enwh", "wh_del", "consumption_wh", "energy_wh"),
    },
    "exported_to_grid_wh": {
        "label": "exported",
        "endpoint": "energy_export_telemetry",
        "interval_fields": ("wh_exported", "wh_del", "export_wh", "energy_wh"),
    },
    "imported_from_grid_wh": {
        "label": "imported",
        "endpoint": "energy_import_telemetry",
        "interval_fields": ("wh_imported", "wh_del", "import_wh", "energy_wh"),
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
    rows = query_rows(client, query)
    if not rows:
        raise RuntimeError("No imported history rows found.")
    return rows[0]["_time"].astimezone(local_tz).date()


def local_history_totals(
    client: InfluxDBClient,
    bucket: str,
    local_day: date,
    local_tz: ZoneInfo,
) -> dict[str, float]:
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=local_tz)
    stop = start + timedelta(days=1)
    field_filter = " or ".join(f'r._field == "{field}"' for field in HISTORY_FIELDS)
    query = f"""
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{HISTORY_MEASUREMENT}" and ({field_filter}))
  |> group(columns: ["_field"])
  |> sum()
  |> keep(columns: ["_field", "_value"])
"""
    totals = {field: 0.0 for field in HISTORY_FIELDS}
    for row in query_rows(client, query):
        totals[str(row["_field"])] = float(row["_value"])
    return totals


def interval_sum(payload: dict[str, Any], fields: tuple[str, ...]) -> tuple[float | None, str]:
    intervals = payload.get("intervals")
    if not isinstance(intervals, list):
        return None, ""
    if intervals and all(isinstance(item, list) for item in intervals):
        intervals = [child for item in intervals for child in item]
    for field in fields:
        values = [
            float(item[field])
            for item in intervals
            if isinstance(item, dict) and isinstance(item.get(field), (int, float))
        ]
        if values:
            return sum(values), field
    return None, ""


def fetch_cloud_telemetry(system_id: str, local_day: date, local_tz: ZoneInfo) -> dict[str, dict[str, Any]]:
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=local_tz)
    stop = start + timedelta(days=1)
    params = {"start_at": str(int(start.timestamp())), "end_at": str(int(stop.timestamp()))}
    results: dict[str, dict[str, Any]] = {}
    for field, config in HISTORY_FIELDS.items():
        path = f"/systems/{system_id}/{config['endpoint']}"
        response = cloud_get(path, params=params)
        result: dict[str, Any] = {
            "path": path,
            "params": params,
            "status_code": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "cloud_wh": None,
            "interval_field": "",
            "interval_count": None,
        }
        try:
            payload = response.json()
            result["summary"] = compact(redact(payload))
            if isinstance(payload, dict):
                intervals = payload.get("intervals")
                result["interval_count"] = len(intervals) if isinstance(intervals, list) else None
                cloud_wh, interval_field = interval_sum(payload, config["interval_fields"])
                result["cloud_wh"] = cloud_wh
                result["interval_field"] = interval_field
        except ValueError:
            result["summary"] = response.text[:500]
        results[field] = result
    return results


def main() -> None:
    system_id = env_required("ENPHASE_CLOUD_SYSTEM_ID")
    bucket = os.environ["INFLUXDB_BUCKET"]
    local_tz = ZoneInfo(os.getenv("ENPHASE_HISTORY_TIMEZONE", "UTC"))
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )

    local_day = last_history_date(client, bucket, local_tz)
    local_totals = local_history_totals(client, bucket, local_day, local_tz)
    cloud_results = fetch_cloud_telemetry(system_id, local_day, local_tz)

    print(f"system_id={system_id}")
    print(f"history_day={local_day.isoformat()} timezone={local_tz.key}")
    comparisons = []
    for field, config in HISTORY_FIELDS.items():
        local_wh = local_totals[field]
        result = cloud_results[field]
        cloud_wh = result.get("cloud_wh")
        difference = None if cloud_wh is None else cloud_wh - local_wh
        comparison = {
            "label": config["label"],
            "history_field": field,
            "local_wh": local_wh,
            "cloud_wh": cloud_wh,
            "difference_wh": difference,
            "difference_percent": None if not local_wh or difference is None else difference / local_wh * 100.0,
            "cloud_status_code": result["status_code"],
            "interval_count": result["interval_count"],
            "interval_field": result["interval_field"],
        }
        comparisons.append(comparison)
        cloud_text = "unavailable" if cloud_wh is None else f"{cloud_wh / 1000.0:.3f} kWh"
        diff_text = "n/a" if difference is None else f"{difference / 1000.0:+.3f} kWh"
        print(
            f"{config['label']}: local={local_wh / 1000.0:.3f} kWh "
            f"cloud={cloud_text} diff={diff_text} "
            f"status={result['status_code']} intervals={result['interval_count']} field={result['interval_field']}"
        )

    output_path, latest_path = write_json_snapshot(
        "history_telemetry_compare",
        {
            "system_id": system_id,
            "history_day": local_day.isoformat(),
            "timezone": local_tz.key,
            "comparisons": comparisons,
            "cloud_results": cloud_results,
        },
    )
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
