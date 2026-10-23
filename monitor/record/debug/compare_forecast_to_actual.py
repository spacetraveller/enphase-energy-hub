import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import meter_eid_for_role  # noqa: E402


METRICS = ["Produced", "Consumed", "Exported", "Imported"]


@dataclass
class Delta:
    metric: str
    kwh: float
    first_time: datetime | None
    last_time: datetime | None
    source: str


def query_rows(client: InfluxDBClient, query: str) -> list[dict]:
    rows = []
    for table in client.query_api().query(query):
        for record in table.records:
            rows.append(record.values)
    return rows


def first_last_delta(
    client: InfluxDBClient,
    bucket: str,
    source: str,
    metric: str,
    filters: str,
    field: str,
    start: str,
    stop: str,
) -> Delta:
    query = f"""
data = from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => {filters} and r._field == "{field}")
  |> keep(columns: ["_time", "_value"])
  |> group()

first = data |> first() |> set(key: "point", value: "first")
last = data |> last() |> set(key: "point", value: "last")

union(tables: [first, last])
"""
    values: dict[str, dict] = {}
    for row in query_rows(client, query):
        values[str(row.get("point"))] = row
    if "first" not in values or "last" not in values:
        return Delta(metric=metric, kwh=0.0, first_time=None, last_time=None, source=source)
    first_value = float(values["first"].get("_value") or 0.0)
    last_value = float(values["last"].get("_value") or 0.0)
    return Delta(
        metric=metric,
        kwh=(last_value - first_value) / 1000.0,
        first_time=values["first"].get("_time"),
        last_time=values["last"].get("_time"),
        source=source,
    )


def main() -> None:
    production_eid = meter_eid_for_role("production")
    net_eid = meter_eid_for_role("net")
    bucket = os.environ["INFLUXDB_BUCKET"]
    timezone_name = os.getenv("COMPARE_TIMEZONE", "UTC")
    local_tz = ZoneInfo(timezone_name)
    now = datetime.now(local_tz)
    target_date = os.getenv("COMPARE_TARGET_DATE", now.date().isoformat())
    start = f"{target_date}T00:00:00+10:00"
    stop = now.isoformat()
    completed_hour = max(now.hour - 1, -1)

    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )

    actual_deltas = [
        first_last_delta(
            client,
            bucket,
            "production meter act_energy_dlvd",
            "Produced",
            f'r._measurement == "enphase_meter_reading" and r.eid == "{production_eid}"',
            "act_energy_dlvd",
            start,
            stop,
        ),
        first_last_delta(
            client,
            bucket,
            "energy_flow total-consumption wh_lifetime",
            "Consumed",
            'r._measurement == "enphase_energy_flow" and r.section == "consumption" and r.measurement_type == "total-consumption"',
            "wh_lifetime",
            start,
            stop,
        ),
        first_last_delta(
            client,
            bucket,
            "net meter act_energy_rcvd",
            "Exported",
            f'r._measurement == "enphase_meter_reading" and r.eid == "{net_eid}"',
            "act_energy_rcvd",
            start,
            stop,
        ),
        first_last_delta(
            client,
            bucket,
            "net meter act_energy_dlvd",
            "Imported",
            f'r._measurement == "enphase_meter_reading" and r.eid == "{net_eid}"',
            "act_energy_dlvd",
            start,
            stop,
        ),
    ]
    actuals = {delta.metric: delta.kwh for delta in actual_deltas}
    first_actual_time = min(
        (delta.first_time for delta in actual_deltas if delta.first_time is not None),
        default=None,
    )

    forecast_daily_query = f"""
from(bucket: "{bucket}")
  |> range(start: {target_date}T00:00:00+10:00, stop: {target_date}T23:59:59+10:00)
  |> filter(fn: (r) => r._measurement == "enphase_prediction_daily" and r.target_date == "{target_date}" and r.model == "weather_adjusted")
  |> filter(fn: (r) => r._field == "likely_kwh")
  |> group(columns: ["metric"])
  |> last()
"""
    daily_forecast = {metric: 0.0 for metric in METRICS}
    for row in query_rows(client, forecast_daily_query):
        metric = str(row.get("metric"))
        if metric in daily_forecast:
            daily_forecast[metric] = float(row.get("_value") or 0.0)

    forecast_hourly_query = f"""
from(bucket: "{bucket}")
  |> range(start: {target_date}T00:00:00+10:00, stop: {stop})
  |> filter(fn: (r) => r._measurement == "enphase_prediction_hourly" and r.target_date == "{target_date}" and r.model == "weather_adjusted")
  |> filter(fn: (r) => r._field == "kwh")
  |> group(columns: ["metric"])
  |> sum()
"""
    elapsed_forecast = {metric: 0.0 for metric in METRICS}
    for row in query_rows(client, forecast_hourly_query):
        metric = str(row.get("metric"))
        if metric in elapsed_forecast:
            elapsed_forecast[metric] = float(row.get("_value") or 0.0)

    fair_start = (
        first_actual_time.astimezone(local_tz).isoformat()
        if first_actual_time is not None
        else start
    )
    fair_forecast_query = f"""
from(bucket: "{bucket}")
  |> range(start: {fair_start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "enphase_prediction_hourly" and r.target_date == "{target_date}" and r.model == "weather_adjusted")
  |> filter(fn: (r) => r._field == "kwh")
  |> group(columns: ["metric"])
  |> sum()
"""
    fair_forecast = {metric: 0.0 for metric in METRICS}
    for row in query_rows(client, fair_forecast_query):
        metric = str(row.get("metric"))
        if metric in fair_forecast:
            fair_forecast[metric] = float(row.get("_value") or 0.0)

    print(f"target_date={target_date} timezone={timezone_name} now={now.isoformat()}")
    print(f"completed_hour={completed_hour}")
    if first_actual_time:
        print(f"actuals_start={first_actual_time.astimezone(local_tz).isoformat()}")
    print("")
    print("actual_sources:")
    for delta in actual_deltas:
        first = delta.first_time.astimezone(local_tz).isoformat() if delta.first_time else None
        last = delta.last_time.astimezone(local_tz).isoformat() if delta.last_time else None
        print(f"{delta.metric}: {delta.source}; first={first}; last={last}")

    print("")
    print("metric actual_so_far_kwh fair_forecast_kwh midnight_forecast_kwh full_day_forecast_kwh fair_error_kwh fair_error_pct")
    for metric in METRICS:
        actual = actuals[metric]
        forecast_elapsed = fair_forecast[metric]
        forecast_daily = daily_forecast[metric]
        error = actual - forecast_elapsed
        error_pct = (error / forecast_elapsed * 100.0) if forecast_elapsed else 0.0
        print(
            f"{metric} {actual:.2f} {forecast_elapsed:.2f} {elapsed_forecast[metric]:.2f} {forecast_daily:.2f} {error:+.2f} {error_pct:+.1f}%"
        )


if __name__ == "__main__":
    main()
