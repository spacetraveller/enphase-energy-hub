import argparse
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DASHBOARD = PROJECT_ROOT / "grafana" / "dashboards" / "energy-flow.json"
DEFAULT_TIERS = PROJECT_ROOT / "grafana" / "solar-power-tiers.json"

PANEL_ID = 26
PANEL_TITLE = "Solar Powers"


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, data):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def build_mappings(tiers):
    ordered = sorted(tiers, key=lambda tier: tier["min_watts"])
    mappings = []
    for index, tier in enumerate(ordered):
        next_tier = ordered[index + 1] if index + 1 < len(ordered) else None
        upper_bound = float(next_tier["min_watts"]) - 0.001 if next_tier else 999999.0
        mappings.append(
            {
                "options": {
                    "from": float(tier["min_watts"]),
                    "result": {"text": tier["label"]},
                    "to": upper_bound,
                },
                "type": "range",
            }
        )
    return mappings


def build_query():
    return (
        'from(bucket: "enphase")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r._measurement == "enphase_energy_flow" and r.section == "production" and r.measurement_type == "production" and r._field == "w_now")\n'
        '  |> group(columns: ["_field"])\n'
        "  |> last()\n"
        '  |> map(fn: (r) => ({ r with _field: "Solar powers", _value: float(v: r._value) }))'
    )


def build_panel(tiers):
    return {
        "datasource": {"type": "influxdb", "uid": "InfluxDB"},
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "mappings": build_mappings(tiers),
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "green", "value": None}],
                },
                "unit": "none",
            },
            "overrides": [],
        },
        "gridPos": {"h": 7, "w": 6, "x": 6, "y": 66},
        "id": PANEL_ID,
        "options": {
            "colorMode": "none",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "",
                "values": False,
            },
            "showPercentChange": False,
            "textMode": "value",
            "wideLayout": True,
        },
        "pluginVersion": "11.6.1",
        "targets": [
            {
                "datasource": {"type": "influxdb", "uid": "InfluxDB"},
                "query": build_query(),
                "refId": "A",
            }
        ],
        "title": PANEL_TITLE,
        "type": "stat",
    }


def find_panel_index(panels, *, title=None, panel_id=None):
    for index, panel in enumerate(panels):
        if panel_id is not None and panel.get("id") == panel_id:
            return index
        if title is not None and panel.get("title") == title:
            return index
    return None


def remove_existing_solar_panel(panels):
    existing_index = find_panel_index(panels, panel_id=PANEL_ID)
    if existing_index is not None:
        panels.pop(existing_index)

    for panel in panels:
        nested_panels = panel.get("panels")
        if nested_panels:
            remove_existing_solar_panel(nested_panels)


def find_panel_container(panels, title):
    for panel in panels:
        if panel.get("title") == title:
            return panels

        nested_panels = panel.get("panels")
        if nested_panels:
            found = find_panel_container(nested_panels, title)
            if found is not None:
                return found

    return None


def upsert_panel(dashboard, panel):
    remove_existing_solar_panel(dashboard["panels"])

    panels = find_panel_container(dashboard["panels"], "Voltage") or dashboard["panels"]
    voltage_index = find_panel_index(panels, title="Voltage")
    if voltage_index is None:
        panels.append(panel)
    else:
        panels.insert(voltage_index + 1, panel)
    return "upserted"


def main():
    parser = argparse.ArgumentParser(description="Update the Energy Flow Solar Powers panel from wattage tiers.")
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--tiers", type=Path, default=DEFAULT_TIERS)
    args = parser.parse_args()

    dashboard = load_json(args.dashboard)
    tier_config = load_json(args.tiers)
    action = upsert_panel(dashboard, build_panel(tier_config["tiers"]))
    write_json(args.dashboard, dashboard)
    print(f"{action} {PANEL_TITLE} panel in {args.dashboard}")


if __name__ == "__main__":
    main()
