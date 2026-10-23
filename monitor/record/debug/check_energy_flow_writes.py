import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor import (  # noqa: E402
    ENERGY_FLOW_INTEGER_FIELDS,
    ENERGY_FLOW_MEASUREMENT,
    env_int,
    env_required,
    fetch_json,
    field_name,
)


def main() -> None:
    timeout = env_int("PRODUCTION_TIMEOUT_SECONDS", 15)
    path = os.getenv("ENERGY_FLOW_PATH", "/production.json")
    timestamp = datetime.now(timezone.utc)

    try:
        details = fetch_json(path, timeout)
    except requests.RequestException as exc:
        print(f"fetch_error={exc.__class__.__name__}", flush=True)
        return

    client = InfluxDBClient(
        url=env_required("INFLUXDB_URL"),
        token=env_required("INFLUXDB_TOKEN"),
        org=env_required("INFLUXDB_ORG"),
    )
    write_api = client.write_api(write_options=SYNCHRONOUS)
    bucket = env_required("INFLUXDB_BUCKET")
    org = env_required("INFLUXDB_ORG")

    for section in ("production", "consumption", "storage"):
        for item in details.get(section, []):
            tags = {
                "section": section,
                "type": str(item.get("type", "")),
                "measurement_type": str(item.get("measurementType", "")),
            }
            label = ",".join(f"{key}={value}" for key, value in tags.items() if value)
            for source in ("activeCount", "readingTime", "wNow", "whToday"):
                if source not in item:
                    continue
                field = field_name(source)
                raw_value = item[source]
                value = int(float(raw_value)) if field in ENERGY_FLOW_INTEGER_FIELDS else float(raw_value)
                kind = "int" if isinstance(value, int) else "float"
                point = Point(ENERGY_FLOW_MEASUREMENT).time(timestamp, WritePrecision.NS)
                for key, tag_value in tags.items():
                    point.tag(key, tag_value)
                point.field(field, value)
                try:
                    write_api.write(bucket=bucket, org=org, record=point)
                    print(f"ok {ENERGY_FLOW_MEASUREMENT} {label} {field} {kind}", flush=True)
                except ApiException as exc:
                    body = exc.body or ""
                    print(
                        f"fail {ENERGY_FLOW_MEASUREMENT} {label} {field} {kind} status={exc.status} {body[:160]}",
                        flush=True,
                    )


if __name__ == "__main__":
    main()
