import os
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient


def main() -> None:
    measurement = os.environ["DELETE_MEASUREMENT"]
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )
    client.delete_api().delete(
        start=datetime(1970, 1, 1, tzinfo=timezone.utc),
        stop=datetime(2100, 1, 1, tzinfo=timezone.utc),
        predicate=f'_measurement="{measurement}"',
        bucket=os.environ["INFLUXDB_BUCKET"],
        org=os.environ["INFLUXDB_ORG"],
    )
    print(f'Deleted measurement "{measurement}"')


if __name__ == "__main__":
    main()
