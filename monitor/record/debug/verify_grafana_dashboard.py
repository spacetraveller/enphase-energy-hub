"""Read a provisioned dashboard and execute its Flux queries without modifying it."""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.query_helpers import influx_client, influx_org, query_rows, flux_time, env_timezone


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get("panels", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--grafana-url", default="http://grafana:3000")
    parser.add_argument("--credentials-env", type=Path)
    parser.add_argument("--run-queries", action="store_true")
    args = parser.parse_args()
    expected = json.loads(args.dashboard.read_text())
    dashboard = expected
    if args.live:
        env = {}
        if args.credentials_env:
            for line in args.credentials_env.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() in ("GRAFANA_USERNAME", "GRAFANA_PASSWORD"):
                    env[key.strip()] = value.strip().strip('"').strip("'")
        response = requests.get(f"{args.grafana_url}/api/dashboards/uid/{expected['uid']}",
                                auth=(env.get("GRAFANA_USERNAME", "admin"), env.get("GRAFANA_PASSWORD", "admin")), timeout=15)
        response.raise_for_status()
        dashboard = response.json()["dashboard"]
        for key in ("title", "panels", "timezone", "time"):
            if dashboard[key] != expected[key]:
                raise RuntimeError(f"Served dashboard differs from file: {key}")
    results = []
    if args.run_queries:
        today = datetime.now(ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start, stop = today + timedelta(days=1), today + timedelta(days=4)
        client = influx_client()
        try:
            for panel in panels(dashboard["panels"]):
                for target in panel.get("targets", []):
                    if not target.get("query"):
                        continue
                    query = target["query"].replace("v.timeRangeStart", flux_time(start)).replace("v.timeRangeStop", flux_time(stop))
                    rows = query_rows(client, query, org=influx_org())
                    results.append({"panel": panel["title"], "ref": target["refId"], "rows": len(rows),
                                    "sample": [{k: v for k, v in row.items() if k not in ("result", "table", "_start", "_stop")}
                                               for row in rows[:2]]})
        finally:
            client.close()
    print(json.dumps({"title": dashboard["title"], "uid": dashboard["uid"], "live_matches_file": args.live,
                      "panels": len(list(panels(dashboard["panels"]))), "queries": results}, default=str, indent=2))


if __name__ == "__main__":
    main()
