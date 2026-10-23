import argparse
import json
import os
from pathlib import Path

from influxdb_client import InfluxDBClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DASHBOARD = PROJECT_ROOT / "grafana" / "dashboards" / "energy-flow.json"
PANEL_TITLE = "Solar Powers"


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def find_panel(panels, title):
    for panel in panels:
        if panel.get("title") == title:
            return panel

        nested_panels = panel.get("panels")
        if nested_panels:
            found = find_panel(nested_panels, title)
            if found is not None:
                return found

    return None


def main():
    parser = argparse.ArgumentParser(description="Run the Energy Flow Solar Powers panel query.")
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--range-start", default="-10m")
    args = parser.parse_args()

    dashboard = load_json(args.dashboard)
    panel = find_panel(dashboard["panels"], PANEL_TITLE)
    if panel is None:
        raise SystemExit(f"{PANEL_TITLE} panel was not found")

    query = panel["targets"][0]["query"].replace(
        "range(start: v.timeRangeStart, stop: v.timeRangeStop)",
        f"range(start: {args.range_start})",
    )
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )
    tables = client.query_api().query(query)
    for table in tables:
        for record in table.records:
            print({"field": record.get_field(), "value": record.get_value()})


if __name__ == "__main__":
    main()
