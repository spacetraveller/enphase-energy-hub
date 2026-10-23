import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

from cloud_common import cloud_get, compact, env_required, local_history_timezone, redact, write_json_snapshot


HISTORY_MEASUREMENT = "enphase_history_15m"
HISTORY_FIELDS = {
    "energy_produced_wh": {
        "label": "produced",
        "endpoint": "energy_lifetime",
        "payload_keys": ("production",),
    },
    "energy_consumed_wh": {
        "label": "consumed",
        "endpoint": "consumption_lifetime",
        "payload_keys": ("consumption",),
    },
    "exported_to_grid_wh": {
        "label": "exported",
        "endpoint": "energy_export_lifetime",
        "payload_keys": ("export", "exports", "energy_export", "exported"),
    },
    "imported_from_grid_wh": {
        "label": "imported",
        "endpoint": "energy_import_lifetime",
        "payload_keys": ("import", "imports", "energy_import", "imported"),
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
    last_time = rows[0]["_time"]
    return last_time.astimezone(local_tz).date()


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


def payload_value_for_day(payload: dict[str, Any], payload_keys: tuple[str, ...], target_day: date) -> float | None:
    raw_start = payload.get("start_date")
    if not raw_start:
        return None
    try:
        start_day = date.fromisoformat(str(raw_start))
    except ValueError:
        return None
    offset = (target_day - start_day).days
    if offset < 0:
        return None

    for key in payload_keys:
        values = payload.get(key)
        if isinstance(values, list) and offset < len(values):
            return float(values[offset])
        if isinstance(values, (int, float)) and offset == 0:
            return float(values)
    return None


def fetch_cloud_daily(system_id: str, local_day: date) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for field, config in HISTORY_FIELDS.items():
        endpoint = config["endpoint"]
        path = f"/systems/{system_id}/{endpoint}"
        params = {"start_date": local_day.isoformat(), "end_date": local_day.isoformat()}
        response = cloud_get(path, params=params)
        result: dict[str, Any] = {
            "path": path,
            "params": params,
            "status_code": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "cloud_wh": None,
        }
        try:
            payload = response.json()
            result["summary"] = compact(redact(payload))
            if isinstance(payload, dict):
                result["cloud_wh"] = payload_value_for_day(payload, config["payload_keys"], local_day)
        except ValueError:
            result["summary"] = response.text[:500]
        results[field] = result
    return results


def main() -> None:
    system_id = env_required("ENPHASE_CLOUD_SYSTEM_ID")
    bucket = os.environ["INFLUXDB_BUCKET"]
    local_tz = local_history_timezone()
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )

    local_day = last_history_date(client, bucket, local_tz)
    local_totals = local_history_totals(client, bucket, local_day, local_tz)
    cloud_results = fetch_cloud_daily(system_id, local_day)

    comparisons = []
    print(f"system_id={system_id}")
    print(f"history_day={local_day.isoformat()} timezone={local_tz.key}")
    for field, config in HISTORY_FIELDS.items():
        local_wh = local_totals[field]
        cloud_wh = cloud_results[field].get("cloud_wh")
        difference = None if cloud_wh is None else cloud_wh - local_wh
        comparison = {
            "label": config["label"],
            "history_field": field,
            "local_wh": local_wh,
            "cloud_wh": cloud_wh,
            "difference_wh": difference,
            "difference_percent": None if not local_wh or difference is None else difference / local_wh * 100.0,
            "cloud_status_code": cloud_results[field]["status_code"],
        }
        comparisons.append(comparison)
        cloud_text = "unavailable" if cloud_wh is None else f"{cloud_wh / 1000.0:.3f} kWh"
        diff_text = "n/a" if difference is None else f"{difference / 1000.0:+.3f} kWh"
        print(
            f"{config['label']}: local={local_wh / 1000.0:.3f} kWh "
            f"cloud={cloud_text} diff={diff_text} status={cloud_results[field]['status_code']}"
        )

    output_path, latest_path = write_json_snapshot(
        "history_cloud_compare",
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
