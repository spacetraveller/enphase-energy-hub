import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import influx_bucket, influx_client, influx_org, query_rows  # noqa: E402


def main() -> None:
    bucket = influx_bucket()
    client = influx_client()
    query = f"""
import "influxdata/influxdb/schema"

schema.measurements(bucket: "{bucket}")
"""
    try:
        measurements = [str(row.get("_value")) for row in query_rows(client, query, org=influx_org())]
    finally:
        client.close()

    for measurement in sorted(measurements):
        print(measurement)


if __name__ == "__main__":
    main()
