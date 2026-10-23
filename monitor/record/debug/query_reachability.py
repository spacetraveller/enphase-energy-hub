import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import influx_bucket, influx_client, influx_org, query_rows  # noqa: E402


def main() -> None:
    query = f"""
from(bucket: "{influx_bucket()}")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "enphase_reachability")
  |> filter(fn: (r) => r._field == "reachable" or r._field == "response_ms")
  |> last()
"""
    client = influx_client()
    try:
        rows = query_rows(client, query, org=influx_org())
    finally:
        client.close()
    for row in rows:
        print(
            {
                "time": row["_time"].isoformat(),
                "field": row["_field"],
                "value": row["_value"],
                "target": row.get("target"),
                "probe_url": row.get("probe_url"),
            }
        )


if __name__ == "__main__":
    main()
