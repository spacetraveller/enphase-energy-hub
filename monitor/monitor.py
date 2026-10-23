import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests
import urllib3
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")
ENERGY_FLOW_MEASUREMENT = "enphase_energy_flow"
ENERGY_FLOW_INTEGER_FIELDS = {
    "active_count",
    "reading_time",
    "wh_today",
}
ENERGY_FLOW_SKIPPED_FIELDS = {
    "wh_today",
}

def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"{name} must be an integer, using {default}", flush=True)
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def build_url(
    path_env: str = "ENPHASE_PATH",
    scheme_env: str = "ENPHASE_SCHEME",
    default_path: str = "/",
) -> str:
    host = env_required("ENPHASE_HOST").strip("/")
    scheme = os.getenv(scheme_env, os.getenv("ENPHASE_SCHEME", "http")).strip(":/")
    path = os.getenv(path_env, default_path)
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{scheme}://{host}{path}"


def build_reachability_url() -> str:
    explicit_url = os.getenv("ENPHASE_REACHABILITY_URL")
    if explicit_url:
        return explicit_url

    path = os.getenv("ENPHASE_REACHABILITY_PATH")
    if path:
        return build_api_url(path)

    return build_url()


def reachability_target(url: str) -> str:
    return os.getenv("ENPHASE_REACHABILITY_TARGET", url)


def build_api_url(path: str) -> str:
    host = os.getenv("ENPHASE_API_HOST") or env_required("ENPHASE_HOST")
    host = host.strip("/")
    scheme = os.getenv("ENPHASE_API_SCHEME", "https").strip(":/")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{scheme}://{host}{path}"


def probe(url: str, timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = requests.get(
            url,
            timeout=timeout,
            verify=False,
            allow_redirects=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "reachable": 1,
            "response_ms": elapsed_ms,
            "status_code": response.status_code,
            "error": "",
        }
    except requests.RequestException as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "reachable": 0,
            "response_ms": elapsed_ms,
            "status_code": 0,
            "error": exc.__class__.__name__,
        }


def request_headers() -> dict[str, str]:
    token = os.getenv("ENPHASE_ACCESS_TOKEN", "")
    headers = {
        "Accept": "application/json",
        "Connection": "close",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request_host = os.getenv("ENPHASE_REQUEST_HOST", "").strip("/")
    if request_host:
        headers["Host"] = request_host

    return headers


def fetch_json(path: str, timeout: int) -> Any:
    response = requests.get(
        build_api_url(path),
        headers=request_headers(),
        timeout=timeout,
        verify=False,
        allow_redirects=False,
    )
    response.raise_for_status()
    return response.json()


def numeric_value(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def field_name(name: str) -> str:
    return CAMEL_BOUNDARY.sub("_", name).replace("-", "_").lower()


def add_numeric_fields(
    point: Point,
    values: dict[str, Any],
    *,
    integer_fields: set[str] | None = None,
    skip_fields: set[str] | None = None,
    force_float: bool = False,
) -> Point:
    for source, value in values.items():
        if isinstance(value, (dict, list)):
            continue
        field = field_name(source)
        if skip_fields and field in skip_fields:
            continue
        number = numeric_value(value)
        if number is not None:
            if integer_fields and field in integer_fields:
                number = int(number)
            elif force_float:
                number = float(number)
            point.field(field, number)
    return point


def production_points(timestamp: datetime, timeout: int) -> list[Point]:
    points: list[Point] = []
    details: dict[str, Any] | None = None

    if env_bool("PRODUCTION_TOTALS_ENABLED", True):
        summary_path = os.getenv("PRODUCTION_TOTALS_PATH", "/api/v1/production")
        summary = fetch_json(summary_path, timeout)
        totals = Point("enphase_production_totals").time(timestamp, WritePrecision.NS)
        if isinstance(summary, dict) and "production" in summary:
            details = summary
            for item in details.get("production", []):
                if item.get("type") == "eim" or item.get("measurementType") == "production":
                    add_numeric_fields(totals, item)
        else:
            summary_fields = {
                "watts_now": summary.get("wattsNow"),
                "watt_hours_today": summary.get("wattHoursToday"),
                "watt_hours_seven_days": summary.get("wattHoursSevenDays"),
                "watt_hours_lifetime": summary.get("wattHoursLifetime"),
            }
            for field, value in summary_fields.items():
                number = numeric_value(value)
                if number is not None:
                    totals.field(field, number)
        points.append(totals)

    if env_bool("PRODUCTION_INVERTERS_ENABLED", True):
        inverters = fetch_json("/api/v1/production/inverters", timeout)
        for inverter in inverters:
            serial_number = str(inverter.get("serialNumber", "unknown"))
            point = (
                Point("enphase_inverter_production")
                .tag("serial_number", serial_number)
                .tag("dev_type", str(inverter.get("devType", "")))
                .time(timestamp, WritePrecision.NS)
            )
            for field, source in {
                "last_report_watts": "lastReportWatts",
                "max_report_watts": "maxReportWatts",
                "last_report_date": "lastReportDate",
            }.items():
                number = numeric_value(inverter.get(source))
                if number is not None:
                    point.field(field, number)
            points.append(point)

    if env_bool("ENERGY_FLOW_ENABLED", True):
        details_path = os.getenv("ENERGY_FLOW_PATH", "/production.json")
        if details is None or details_path != os.getenv("PRODUCTION_TOTALS_PATH", ""):
            details = fetch_json(details_path, timeout)
        for section in ("production", "consumption", "storage"):
            for item in details.get(section, []):
                point = (
                    Point(ENERGY_FLOW_MEASUREMENT)
                    .tag("section", section)
                    .tag("type", str(item.get("type", "")))
                    .tag("measurement_type", str(item.get("measurementType", "")))
                    .time(timestamp, WritePrecision.NS)
                )
                points.append(
                    add_numeric_fields(
                        point,
                        item,
                        integer_fields=ENERGY_FLOW_INTEGER_FIELDS,
                        skip_fields=ENERGY_FLOW_SKIPPED_FIELDS,
                        force_float=True,
                    )
                )

    if env_bool("METER_READINGS_ENABLED", True):
        meter_readings = fetch_json("/ivp/meters/readings", timeout)
        for reading in meter_readings:
            point = (
                Point("enphase_meter_reading")
                .tag("eid", str(reading.get("eid", "unknown")))
                .time(timestamp, WritePrecision.NS)
            )
            points.append(add_numeric_fields(point, reading))

    return points


def point_identity(point: Point) -> str:
    return point.to_line_protocol().split(" ")[0]


def write_points(write_api: Any, bucket: str, org: str, points: list[Point]) -> None:
    try:
        write_api.write(bucket=bucket, org=org, record=points)
        return
    except ApiException as exc:
        print(
            f"write_batch_failed status={exc.status} reason={exc.reason}; retrying points individually",
            flush=True,
        )

    for point in points:
        try:
            write_api.write(bucket=bucket, org=org, record=point)
        except ApiException as exc:
            print(
                f"write_point_dropped point={point_identity(point)} status={exc.status} reason={exc.reason}",
                flush=True,
            )


def main() -> None:
    url = build_reachability_url()
    target = reachability_target(url)
    interval = env_int("PROBE_INTERVAL_SECONDS", 30)
    production_enabled = env_bool("PRODUCTION_MONITORING_ENABLED", True)
    production_interval = env_int("PRODUCTION_INTERVAL_SECONDS", 60)
    timeout = env_int("PROBE_TIMEOUT_SECONDS", 5)
    production_timeout = env_int("PRODUCTION_TIMEOUT_SECONDS", timeout)
    bucket = env_required("INFLUXDB_BUCKET")
    org = env_required("INFLUXDB_ORG")
    next_production_probe = 0.0

    client = InfluxDBClient(
        url=env_required("INFLUXDB_URL"),
        token=env_required("INFLUXDB_TOKEN"),
        org=org,
    )
    write_api = client.write_api(write_options=SYNCHRONOUS)

    print(f"Probing {url} every {interval}s as {target}", flush=True)
    if production_enabled and os.getenv("ENPHASE_ACCESS_TOKEN"):
        print(
            f"Recording production data every {production_interval}s",
            flush=True,
        )
        print(
            "endpoint_flags totals={totals} inverters={inverters} energy_flow={energy_flow} meter_readings={meter_readings}".format(
                totals=env_bool("PRODUCTION_TOTALS_ENABLED", True),
                inverters=env_bool("PRODUCTION_INVERTERS_ENABLED", True),
                energy_flow=env_bool("ENERGY_FLOW_ENABLED", True),
                meter_readings=env_bool("METER_READINGS_ENABLED", True),
            ),
            flush=True,
        )
    elif production_enabled:
        print("Production monitoring disabled because ENPHASE_ACCESS_TOKEN is empty", flush=True)

    while True:
        now = datetime.now(timezone.utc)
        result = probe(url, timeout)
        point = (
            Point("enphase_reachability")
            .tag("target", target)
            .tag("probe_url", url)
            .field("reachable", result["reachable"])
            .field("response_ms", float(result["response_ms"]))
            .field("status_code", int(result["status_code"]))
            .field("error", result["error"])
            .time(now, WritePrecision.NS)
        )
        points = [point]

        if (
            production_enabled
            and os.getenv("ENPHASE_ACCESS_TOKEN")
            and time.time() >= next_production_probe
        ):
            try:
                production_records = production_points(now, production_timeout)
                points.extend(production_records)
                next_production_probe = time.time() + production_interval
                print(f"production_recorded=true points={len(production_records)}", flush=True)
            except requests.RequestException as exc:
                next_production_probe = time.time() + production_interval
                print(f"production_recorded=false error={exc.__class__.__name__}", flush=True)

        write_points(write_api, bucket, org, points)
        print(
            "reachable={reachable} status={status_code} response_ms={response_ms:.1f} error={error}".format(
                **result
            ),
            flush=True,
        )
        time.sleep(interval)


if __name__ == "__main__":
    main()
