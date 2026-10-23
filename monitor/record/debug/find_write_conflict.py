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
    build_reachability_url,
    env_int,
    env_required,
    production_points,
    probe,
    reachability_target,
)


def describe(point: Point) -> str:
    return point.to_line_protocol()


def single_field_point(source: Point, field: str, value: object, timestamp: datetime) -> Point:
    point = Point(source._name)
    for key, tag_value in source._tags.items():
        point.tag(key, tag_value)
    return point.field(field, value).time(timestamp, WritePrecision.NS)


def main() -> None:
    now = datetime.now(timezone.utc)
    timeout = env_int("PROBE_TIMEOUT_SECONDS", 5)
    production_timeout = env_int("PRODUCTION_TIMEOUT_SECONDS", timeout)
    url = build_reachability_url()
    target = reachability_target(url)

    reachability = probe(url, timeout)
    points = [
        Point("enphase_reachability")
        .tag("target", target)
        .tag("probe_url", url)
        .field("reachable", reachability["reachable"])
        .field("response_ms", float(reachability["response_ms"]))
        .field("status_code", int(reachability["status_code"]))
        .field("error", reachability["error"])
        .time(now, WritePrecision.NS)
    ]

    try:
        points.extend(production_points(now, production_timeout))
    except requests.RequestException as exc:
        print(f"production_fetch_error={exc.__class__.__name__}", flush=True)
        return

    client = InfluxDBClient(
        url=env_required("INFLUXDB_URL"),
        token=env_required("INFLUXDB_TOKEN"),
        org=env_required("INFLUXDB_ORG"),
    )
    write_api = client.write_api(write_options=SYNCHRONOUS)
    bucket = env_required("INFLUXDB_BUCKET")
    org = env_required("INFLUXDB_ORG")

    for index, point in enumerate(points, start=1):
        try:
            write_api.write(bucket=bucket, org=org, record=point)
        except ApiException as exc:
            print(f"conflict_point={index} {describe(point)}", flush=True)
            print(f"status={exc.status} reason={exc.reason}", flush=True)
            print(exc.body, flush=True)
            for field, value in point._fields.items():
                try:
                    write_api.write(
                        bucket=bucket,
                        org=org,
                        record=single_field_point(point, field, value, now),
                    )
                except ApiException as field_exc:
                    print(f"conflict_field={field} value={value!r}", flush=True)
                    print(field_exc.body, flush=True)
            return

    print(f"all_points_wrote={len(points)}", flush=True)


if __name__ == "__main__":
    main()
