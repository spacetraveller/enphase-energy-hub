import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import METER_READING_MEASUREMENT, latest_measurement_rows  # noqa: E402


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Query latest Enphase meter-reading fields with reusable filters."
    )
    parser.add_argument("--start", default="-2h")
    parser.add_argument(
        "--fields",
        default="",
        help="Comma-separated fields. Defaults to all fields seen in the range.",
    )
    parser.add_argument("--eid", default="", help="Optional meter EID filter.")
    parser.add_argument(
        "--positive-only",
        action="store_true",
        help="Only include rows where the selected field value is greater than zero.",
    )
    args = parser.parse_args(argv)

    filters = []
    if args.eid:
        filters.append(f'r.eid == "{args.eid}"')
    if args.positive_only:
        filters.append("r._value > 0.0")

    rows = latest_measurement_rows(
        METER_READING_MEASUREMENT,
        start=args.start,
        fields=parse_csv(args.fields),
        filters=filters,
        group_columns=["eid", "_field"],
        keep_columns=["_time", "eid", "_field", "_value"],
    )
    rows.sort(key=lambda row: (str(row.get("eid") or ""), str(row.get("_field") or "")))
    for row in rows:
        print(
            json.dumps(
                {
                    "eid": row.get("eid"),
                    "field": row.get("_field"),
                    "value": row.get("_value"),
                    "time": row.get("_time").isoformat() if row.get("_time") else None,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
