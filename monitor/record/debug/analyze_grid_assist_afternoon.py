import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import percentile_linear as shared_percentile_linear


LOCAL_TZ = ZoneInfo(os.getenv("HISTORY_TIMEZONE", "UTC"))
CHARGER_KW = float(os.getenv("GRID_ASSIST_CHARGER_KW", "5.5"))
IMPORT_RATE_AUD_PER_KWH = float(os.getenv("IMPORT_RATE_AUD_PER_KWH", "0.3395"))
FEED_IN_RATE_AUD_PER_KWH = float(os.getenv("FEED_IN_RATE_AUD_PER_KWH", "0.01"))
START_HOUR = int(os.getenv("GRID_ASSIST_START_HOUR", "14"))
START_MINUTE = int(os.getenv("GRID_ASSIST_START_MINUTE", "30"))
MONTHS = {
    int(value.strip())
    for value in os.getenv("GRID_ASSIST_MONTHS", "4,5").split(",")
    if value.strip()
}


def query_rows(client: InfluxDBClient, query: str) -> list[dict]:
    rows = []
    for table in client.query_api().query(query):
        for record in table.records:
            rows.append(record.values)
    return rows


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    return shared_percentile_linear(values, pct)


def main() -> None:
    bucket = os.environ["INFLUXDB_BUCKET"]
    org = os.environ["INFLUXDB_ORG"]
    slot_hours = 0.25
    charger_slot_kwh = CHARGER_KW * slot_hours
    net_saving_rate = IMPORT_RATE_AUD_PER_KWH - FEED_IN_RATE_AUD_PER_KWH

    query = f'''
from(bucket: "{bucket}")
  |> range(start: 2024-10-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "enphase_history_15m" and (r._field == "energy_produced_wh" or r._field == "energy_consumed_wh" or r._field == "exported_to_grid_wh" or r._field == "imported_from_grid_wh"))
  |> keep(columns: ["_time", "_field", "_value"])
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=org,
    )
    rows = query_rows(client, query)
    client.close()

    by_day: dict[str, list[dict]] = defaultdict(list)
    slot_rows = []
    for row in rows:
        timestamp = row["_time"].astimezone(LOCAL_TZ)
        if timestamp.month not in MONTHS:
            continue
        if (timestamp.hour, timestamp.minute) < (START_HOUR, START_MINUTE):
            continue
        exported_kwh = float(row.get("exported_to_grid_wh") or 0.0) / 1000.0
        produced_kwh = float(row.get("energy_produced_wh") or 0.0) / 1000.0
        consumed_kwh = float(row.get("energy_consumed_wh") or 0.0) / 1000.0
        imported_kwh = float(row.get("imported_from_grid_wh") or 0.0) / 1000.0
        solar_to_car_kwh = min(exported_kwh, charger_slot_kwh)
        grid_to_car_kwh = charger_slot_kwh - solar_to_car_kwh
        item = {
            "time": timestamp,
            "exported_kwh": exported_kwh,
            "produced_kwh": produced_kwh,
            "consumed_kwh": consumed_kwh,
            "imported_kwh": imported_kwh,
            "solar_to_car_kwh": solar_to_car_kwh,
            "grid_to_car_kwh": grid_to_car_kwh,
        }
        slot_rows.append(item)
        by_day[timestamp.date().isoformat()].append(item)

    day_summaries = []
    for day, items in by_day.items():
        if not items:
            continue
        solar_to_car = sum(item["solar_to_car_kwh"] for item in items)
        grid_to_car = sum(item["grid_to_car_kwh"] for item in items)
        exported = sum(item["exported_kwh"] for item in items)
        produced = sum(item["produced_kwh"] for item in items)
        hours_with_any_solar = sum(1 for item in items if item["solar_to_car_kwh"] > 0) * slot_hours
        hours_over_1kw = sum(1 for item in items if item["exported_kwh"] / slot_hours >= 1.0) * slot_hours
        hours_over_2kw = sum(1 for item in items if item["exported_kwh"] / slot_hours >= 2.0) * slot_hours
        day_summaries.append(
            {
                "date": day,
                "slots": len(items),
                "produced_kwh_after_start": produced,
                "exported_kwh_after_start": exported,
                "solar_to_car_kwh": solar_to_car,
                "grid_to_car_kwh": grid_to_car,
                "effective_solar_kw_mean": solar_to_car / max(len(items) * slot_hours, 0.25),
                "gross_import_avoided_aud": solar_to_car * IMPORT_RATE_AUD_PER_KWH,
                "net_saving_aud": solar_to_car * net_saving_rate,
                "hours_with_any_solar": hours_with_any_solar,
                "hours_over_1kw": hours_over_1kw,
                "hours_over_2kw": hours_over_2kw,
            }
        )

    solar_values = [day["solar_to_car_kwh"] for day in day_summaries]
    saving_values = [day["net_saving_aud"] for day in day_summaries]
    useful_days = [day for day in day_summaries if day["solar_to_car_kwh"] >= 1.0]
    strong_days = [day for day in day_summaries if day["solar_to_car_kwh"] >= 4.0]
    best_days = sorted(day_summaries, key=lambda item: item["solar_to_car_kwh"], reverse=True)[:10]

    by_hour: dict[int, list[dict]] = defaultdict(list)
    for item in slot_rows:
        by_hour[item["time"].hour].append(item)

    hourly = []
    for hour, items in sorted(by_hour.items()):
        solar_slot = [item["solar_to_car_kwh"] for item in items]
        export_kw = [item["exported_kwh"] / slot_hours for item in items]
        hourly.append(
            {
                "hour": f"{hour:02d}:00",
                "samples": len(items),
                "avg_solar_offset_kw": round((statistics.mean(solar_slot) / slot_hours) if solar_slot else 0.0, 2),
                "median_export_kw": round(percentile(export_kw, 0.5), 2),
                "p75_export_kw": round(percentile(export_kw, 0.75), 2),
                "share_slots_any_solar": round(sum(1 for value in solar_slot if value > 0) / len(solar_slot), 2) if solar_slot else 0.0,
                "share_slots_over_2kw": round(sum(1 for value in export_kw if value >= 2.0) / len(export_kw), 2) if export_kw else 0.0,
            }
        )

    print(
        json.dumps(
            {
                "assumptions": {
                    "months": sorted(MONTHS),
                    "start_time": f"{START_HOUR:02d}:{START_MINUTE:02d}",
                    "charger_kw": CHARGER_KW,
                    "charger_slot_kwh": charger_slot_kwh,
                    "import_rate_aud_per_kwh": IMPORT_RATE_AUD_PER_KWH,
                    "feed_in_rate_aud_per_kwh": FEED_IN_RATE_AUD_PER_KWH,
                    "net_saving_rate_aud_per_kwh": net_saving_rate,
                    "method": "Use historical exported_to_grid_wh after start time as solar that could offset a 5.5 kW grid-assisted EV charge. Saving is solar offset times import rate minus foregone feed-in credit.",
                },
                "coverage": {
                    "days": len(day_summaries),
                    "slots": len(slot_rows),
                    "first_day": min((day["date"] for day in day_summaries), default=None),
                    "last_day": max((day["date"] for day in day_summaries), default=None),
                },
                "daily_solar_offset_kwh": {
                    "mean": round(statistics.mean(solar_values), 2) if solar_values else 0.0,
                    "median": round(percentile(solar_values, 0.5), 2),
                    "p25": round(percentile(solar_values, 0.25), 2),
                    "p75": round(percentile(solar_values, 0.75), 2),
                    "p90": round(percentile(solar_values, 0.9), 2),
                },
                "daily_net_saving_aud": {
                    "mean": round(statistics.mean(saving_values), 2) if saving_values else 0.0,
                    "median": round(percentile(saving_values, 0.5), 2),
                    "p25": round(percentile(saving_values, 0.25), 2),
                    "p75": round(percentile(saving_values, 0.75), 2),
                    "p90": round(percentile(saving_values, 0.9), 2),
                },
                "day_hit_rates": {
                    "at_least_1kwh_offset": round(len(useful_days) / len(day_summaries), 2) if day_summaries else 0.0,
                    "at_least_4kwh_offset": round(len(strong_days) / len(day_summaries), 2) if day_summaries else 0.0,
                },
                "hourly": hourly,
                "best_days": [
                    {
                        "date": day["date"],
                        "solar_to_car_kwh": round(day["solar_to_car_kwh"], 2),
                        "net_saving_aud": round(day["net_saving_aud"], 2),
                        "hours_over_2kw": day["hours_over_2kw"],
                    }
                    for day in best_days
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
