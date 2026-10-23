import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    ENERGY_FLOW_MEASUREMENT,
    latest_measurement_rows,
)


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def row_json(row: dict) -> dict:
    return {
        "time": row.get("_time").isoformat() if row.get("_time") else None,
        "section": row.get("section"),
        "measurement_type": row.get("measurement_type"),
        "type": row.get("type"),
        "field": row.get("_field"),
        "value": row.get("_value"),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Query latest Enphase energy-flow rows with reusable filters."
    )
    parser.add_argument("--start", default="-30m")
    parser.add_argument("--fields", default="w_now,wh_today")
    parser.add_argument("--section", default="")
    parser.add_argument("--measurement-type", default="")
    parser.add_argument("--type", default="")
    parser.add_argument(
        "--net-consumption",
        action="store_true",
        help="Shortcut for consumption/net-consumption w_now with import/export split.",
    )
    args = parser.parse_args(argv)

    filters = []
    if args.net_consumption:
        args.fields = "w_now"
        args.section = "consumption"
        args.measurement_type = "net-consumption"
    if args.section:
        filters.append(f'r.section == "{args.section}"')
    if args.measurement_type:
        filters.append(f'r.measurement_type == "{args.measurement_type}"')
    if args.type:
        filters.append(f'r.type == "{args.type}"')

    rows = latest_measurement_rows(
        ENERGY_FLOW_MEASUREMENT,
        start=args.start,
        fields=parse_csv(args.fields),
        filters=filters,
        group_columns=["section", "measurement_type", "type", "_field"],
        keep_columns=["_time", "_field", "_value", "section", "measurement_type", "type"],
    )

    if args.net_consumption:
        output = []
        for row in rows:
            value = float(row.get("_value") or 0.0)
            output.append(
                {
                    "time": row.get("_time").isoformat() if row.get("_time") else None,
                    "net_consumption_w": value,
                    "import_w": value if value > 0 else 0.0,
                    "export_w": -value if value < 0 else 0.0,
                }
            )
        print(json.dumps(output, indent=2))
        return

    for row in rows:
        print(json.dumps(row_json(row), sort_keys=True))


if __name__ == "__main__":
    main()
