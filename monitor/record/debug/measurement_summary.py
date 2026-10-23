import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import influx_bucket, influx_client, influx_org, query_rows  # noqa: E402


def main() -> None:
    bucket = influx_bucket()
    client = influx_client()
    query = f"""
from(bucket: "{bucket}")
  |> range(start: 1970-01-01T00:00:00Z)
  |> keep(columns: ["_measurement", "_time"])
  |> group(columns: ["_measurement"])
  |> reduce(
    identity: {{count: 0, first: time(v: "2100-01-01T00:00:00Z"), last: time(v: "1970-01-01T00:00:00Z")}},
    fn: (r, accumulator) => ({{
      count: accumulator.count + 1,
      first: if r._time < accumulator.first then r._time else accumulator.first,
      last: if r._time > accumulator.last then r._time else accumulator.last,
    }})
  )
  |> sort(columns: ["_measurement"])
"""
    try:
        rows = query_rows(client, query, org=influx_org())
    finally:
        client.close()
    for values in rows:
        print(
            "{measurement} count={count} first={first} last={last}".format(
                measurement=values.get("_measurement"),
                count=values.get("count"),
                first=values.get("first"),
                last=values.get("last"),
            )
        )


if __name__ == "__main__":
    main()
