import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.query_helpers import flux_time, query_history_records


LOCAL_TZ = ZoneInfo(os.getenv("HISTORY_TIMEZONE", "UTC"))
EV_MEASUREMENT = "enphase_ev_charger_telemetry"


def query_rows(client: InfluxDBClient, query: str) -> list[dict]:
    rows = []
    for table in client.query_api().query(query):
        for record in table.records:
            rows.append(record.values)
    return rows


def local_month(value: str) -> tuple[datetime, datetime]:
    start_day = date.fromisoformat(f"{value}-01")
    next_month = (start_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (
        datetime.combine(start_day, datetime.min.time(), tzinfo=LOCAL_TZ),
        datetime.combine(next_month, datetime.min.time(), tzinfo=LOCAL_TZ),
    )


def local_date_range(start_value: str, end_value: str) -> tuple[datetime, datetime]:
    start_day = date.fromisoformat(start_value)
    end_day = date.fromisoformat(end_value)
    if end_day < start_day:
        raise ValueError("--end-date must be on or after --start-date")
    return (
        datetime.combine(start_day, datetime.min.time(), tzinfo=LOCAL_TZ),
        datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL_TZ),
    )


def history_intervals(start: datetime, stop: datetime) -> dict[datetime, dict[str, float]]:
    intervals: dict[datetime, dict[str, float]] = defaultdict(dict)
    records = query_history_records(start=flux_time(start), stop=flux_time(stop))
    for timestamp, field, value_wh in records:
        intervals[timestamp][field] = value_wh / 1000.0
    return intervals


def ev_intervals(client: InfluxDBClient, bucket: str, start: datetime, stop: datetime) -> dict[datetime, float]:
    query = f'''
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "{EV_MEASUREMENT}" and r._field == "consumption_wh")
  |> keep(columns: ["_time", "_value"])
  |> group(columns: ["_time"])
  |> sum()
'''
    return {
        row["_time"]: float(row.get("_value") or 0.0) / 1000.0
        for row in query_rows(client, query)
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate the EV charger's solar and grid energy using aligned 15-minute site telemetry."
    )
    range_group = parser.add_mutually_exclusive_group(required=True)
    range_group.add_argument("--month", help="Local month in YYYY-MM format.")
    range_group.add_argument("--start-date", help="Inclusive local start date in YYYY-MM-DD format.")
    parser.add_argument("--end-date", help="Inclusive local end date; required with --start-date.")
    args = parser.parse_args()

    if args.month:
        if args.end_date:
            parser.error("--end-date can only be used with --start-date")
        start, stop = local_month(args.month)
        range_label = {"month": args.month}
    else:
        if not args.end_date:
            parser.error("--end-date is required with --start-date")
        start, stop = local_date_range(args.start_date, args.end_date)
        range_label = {"start_date": args.start_date, "end_date": args.end_date}
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )
    bucket = os.environ["INFLUXDB_BUCKET"]
    history = history_intervals(start, stop)
    ev = ev_intervals(client, bucket, start, stop)
    client.close()

    active = []
    for timestamp, ev_kwh in sorted(ev.items()):
        if ev_kwh <= 0:
            continue
        site = history.get(timestamp, {})
        production = site.get("energy_produced_wh")
        consumption = site.get("energy_consumed_wh")
        imported = site.get("imported_from_grid_wh")
        exported = site.get("exported_to_grid_wh")
        if None in (production, consumption, imported, exported):
            continue

        solar_used_on_site = min(production, consumption)
        conservative_grid = min(ev_kwh, imported)
        optimistic_solar = min(ev_kwh, solar_used_on_site)
        proportional_solar = (
            min(ev_kwh, solar_used_on_site * ev_kwh / consumption)
            if consumption > 0
            else 0.0
        )
        active.append(
            {
                "time": timestamp,
                "ev_kwh": ev_kwh,
                "production_kwh": production,
                "site_import_kwh": imported,
                "site_export_kwh": exported,
                "conservative_grid_kwh": conservative_grid,
                "conservative_solar_kwh": ev_kwh - conservative_grid,
                "proportional_solar_kwh": proportional_solar,
                "optimistic_solar_kwh": optimistic_solar,
            }
        )

    ev_total = sum(row["ev_kwh"] for row in active)
    conservative_grid = sum(row["conservative_grid_kwh"] for row in active)
    conservative_solar = sum(row["conservative_solar_kwh"] for row in active)
    proportional_solar = sum(row["proportional_solar_kwh"] for row in active)
    optimistic_solar = sum(row["optimistic_solar_kwh"] for row in active)
    proportional_grid = ev_total - proportional_solar
    optimistic_grid = ev_total - optimistic_solar
    by_day: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in active:
        local_day = row["time"].astimezone(LOCAL_TZ).date()
        by_day[local_day]["ev_kwh"] += row["ev_kwh"]
        by_day[local_day]["solar_kwh"] += row["conservative_solar_kwh"]
        by_day[local_day]["grid_kwh"] += row["conservative_grid_kwh"]

    day_summaries = []
    for local_day, values in sorted(by_day.items()):
        ev_kwh = values["ev_kwh"]
        day_summaries.append(
            {
                "date": local_day.isoformat(),
                "ev_kwh": round(ev_kwh, 3),
                "solar_kwh_conservative": round(values["solar_kwh"], 3),
                "grid_kwh_conservative": round(values["grid_kwh"], 3),
                "solar_fraction_conservative": round(values["solar_kwh"] / ev_kwh, 3),
            }
        )

    near_zero_import = [row for row in active if row["site_import_kwh"] <= 0.01]
    solar_producing = [row for row in active if row["production_kwh"] > 0.01]

    print(
        json.dumps(
            {
                **range_label,
                "ev_charger_kwh": round(ev_total, 3),
                "ev_intervals_present": len(ev),
                "active_intervals_with_site_data": len(active),
                "active_days": len({row["time"].astimezone(LOCAL_TZ).date() for row in active}),
                "charging_quality": {
                    "ev_kwh_in_near_zero_import_intervals": round(sum(row["ev_kwh"] for row in near_zero_import), 3),
                    "near_zero_import_energy_fraction": round(sum(row["ev_kwh"] for row in near_zero_import) / ev_total, 3) if ev_total else 0.0,
                    "ev_kwh_while_solar_was_producing": round(sum(row["ev_kwh"] for row in solar_producing), 3),
                    "solar_production_timing_fraction": round(sum(row["ev_kwh"] for row in solar_producing) / ev_total, 3) if ev_total else 0.0,
                    "days_at_least_80pct_solar_conservative": sum(day["solar_fraction_conservative"] >= 0.8 for day in day_summaries),
                    "days_below_50pct_solar_conservative": sum(day["solar_fraction_conservative"] < 0.5 for day in day_summaries),
                },
                "estimated_sources": {
                    "conservative_solar_first_for_house": {
                        "solar_kwh": round(conservative_solar, 3),
                        "grid_kwh": round(conservative_grid, 3),
                        "solar_fraction": round(conservative_solar / ev_total, 3) if ev_total else 0.0,
                    },
                    "proportional_site_mix": {
                        "solar_kwh": round(proportional_solar, 3),
                        "grid_kwh": round(proportional_grid, 3),
                        "solar_fraction": round(proportional_solar / ev_total, 3) if ev_total else 0.0,
                    },
                    "optimistic_solar_first_for_ev": {
                        "solar_kwh": round(optimistic_solar, 3),
                        "grid_kwh": round(optimistic_grid, 3),
                        "solar_fraction": round(optimistic_solar / ev_total, 3) if ev_total else 0.0,
                    },
                },
                "best_days_conservative": sorted(day_summaries, key=lambda row: (row["solar_fraction_conservative"], row["ev_kwh"]), reverse=True)[:5],
                "most_grid_assisted_days": sorted(day_summaries, key=lambda row: (row["solar_fraction_conservative"], -row["ev_kwh"]))[:5],
                "notes": [
                    "EV charger energy is measured directly.",
                    "Solar-versus-grid attribution is estimated because the site meter combines EV and non-EV household loads.",
                    "The conservative estimate allocates grid import to the EV first. The optimistic estimate allocates on-site solar to the EV first. The proportional estimate shares the site's energy mix across loads.",
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
