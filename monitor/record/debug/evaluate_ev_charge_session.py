import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

sys.path.append(str(Path(__file__).resolve().parents[2]))
from record.query_helpers import (  # noqa: E402
    flux_time,
    influx_bucket,
    influx_client,
    influx_org,
    meter_eid_for_role,
    query_rows,
)


LOCAL_TZ = ZoneInfo(os.getenv("HISTORY_TIMEZONE", "UTC"))
IMPORT_RATE_AUD_PER_KWH = float(os.getenv("IMPORT_RATE_AUD_PER_KWH", "0.3395"))
FEED_IN_RATE_AUD_PER_KWH = float(os.getenv("FEED_IN_RATE_AUD_PER_KWH", "0.01"))


def parse_local_datetime(value: str, default_day: date) -> datetime:
    if "T" in value:
        return datetime.fromisoformat(value).replace(tzinfo=LOCAL_TZ)
    hour, minute = [int(part) for part in value.split(":", 1)]
    return datetime.combine(default_day, time(hour, minute), tzinfo=LOCAL_TZ)


def ev_intervals(client: InfluxDBClient, bucket: str, start: datetime, stop: datetime) -> list[dict]:
    query = f'''
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "enphase_ev_charger_telemetry")
  |> filter(fn: (r) => r._field == "consumption_wh")
  |> keep(columns: ["_time", "_value", "serial_no"])
  |> group()
  |> sort(columns: ["_time"])
'''
    rows = []
    for row in query_rows(client, query, org=influx_org()):
        timestamp = row["_time"].astimezone(LOCAL_TZ)
        wh = float(row.get("_value") or 0.0)
        rows.append(
            {
                "start": timestamp,
                "end": timestamp + timedelta(minutes=15),
                "kwh": wh / 1000.0,
                "kw": wh / 1000.0 * 4.0,
            }
        )
    return rows


def meter_delta(
    client: InfluxDBClient,
    bucket: str,
    start: datetime,
    stop: datetime,
    filters: str,
    field: str,
) -> dict:
    query = f'''
data = from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => {filters} and r._field == "{field}")
  |> keep(columns: ["_time", "_value"])
  |> group()

first = data |> first() |> set(key: "point", value: "first")
last = data |> last() |> set(key: "point", value: "last")

union(tables: [first, last])
'''
    values = {}
    for row in query_rows(client, query, org=influx_org()):
        values[str(row.get("point"))] = row
    if "first" not in values or "last" not in values:
        return {"kwh": 0.0, "first": None, "last": None}
    first = float(values["first"].get("_value") or 0.0)
    last = float(values["last"].get("_value") or 0.0)
    return {
        "kwh": (last - first) / 1000.0,
        "first": values["first"].get("_time").astimezone(LOCAL_TZ).isoformat(),
        "last": values["last"].get("_time").astimezone(LOCAL_TZ).isoformat(),
    }


def infer_session_bounds(intervals: list[dict], stop: datetime) -> tuple[datetime, datetime]:
    positive = [row for row in intervals if row["kwh"] > 0.01 and row["start"] < stop]
    if not positive:
        return stop, stop
    session = [positive[-1]]
    previous_end = positive[-1]["start"]
    for row in reversed(positive[:-1]):
        gap_minutes = (previous_end - row["end"]).total_seconds() / 60.0
        if gap_minutes > 30:
            break
        session.append(row)
        previous_end = row["start"]
    starts = [row["start"] for row in session]
    ends = [min(row["end"], stop) for row in session]
    return min(starts), max(ends)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an EV charge session cost from EVSE telemetry and meter deltas.")
    parser.add_argument("--date", default=datetime.now(LOCAL_TZ).date().isoformat())
    parser.add_argument("--start", default="", help="Local start time HH:MM or full ISO. Defaults to latest detected session.")
    parser.add_argument("--stop", default="15:00", help="Local stop time HH:MM or full ISO.")
    args = parser.parse_args()

    day = date.fromisoformat(args.date)
    stop = parse_local_datetime(args.stop, day)
    search_start = datetime.combine(day, time(0, 0), tzinfo=LOCAL_TZ)
    start_arg = parse_local_datetime(args.start, day) if args.start else None

    client = influx_client()
    bucket = influx_bucket()

    try:
        day_intervals = ev_intervals(client, bucket, search_start, stop)
        if start_arg is None:
            start, inferred_stop = infer_session_bounds(day_intervals, stop)
            stop = min(stop, inferred_stop)
        else:
            start = start_arg

        intervals = ev_intervals(client, bucket, start, stop)
        ev_kwh = sum(row["kwh"] for row in intervals)
        active_intervals = [row for row in intervals if row["kwh"] > 0.01]
        production_eid = meter_eid_for_role("production")
        net_eid = meter_eid_for_role("net")
        production = meter_delta(
            client,
            bucket,
            start,
            stop,
            f'r._measurement == "enphase_meter_reading" and r.eid == "{production_eid}"',
            "act_energy_dlvd",
        )
        imported = meter_delta(
            client,
            bucket,
            start,
            stop,
            f'r._measurement == "enphase_meter_reading" and r.eid == "{net_eid}"',
            "act_energy_dlvd",
        )
        exported = meter_delta(
            client,
            bucket,
            start,
            stop,
            f'r._measurement == "enphase_meter_reading" and r.eid == "{net_eid}"',
            "act_energy_rcvd",
        )
    finally:
        client.close()

    site_import_kwh = imported["kwh"]
    grid_fraction = site_import_kwh / ev_kwh if ev_kwh > 0 else 0.0
    solar_assist_kwh = max(ev_kwh - site_import_kwh, 0.0)
    actual_import_cost = site_import_kwh * IMPORT_RATE_AUD_PER_KWH
    pure_grid_cost = ev_kwh * IMPORT_RATE_AUD_PER_KWH
    avoided_import_saving = pure_grid_cost - actual_import_cost
    foregone_feed_in = solar_assist_kwh * FEED_IN_RATE_AUD_PER_KWH
    net_saving_vs_grid = avoided_import_saving - foregone_feed_in

    print(
        json.dumps(
            {
                "session": {
                    "start": start.isoformat(),
                    "stop": stop.isoformat(),
                    "active_intervals": len(active_intervals),
                    "intervals": [
                        {
                            "start": row["start"].isoformat(),
                            "end": row["end"].isoformat(),
                            "kwh": round(row["kwh"], 3),
                            "kw": round(row["kw"], 2),
                        }
                        for row in intervals
                        if row["kwh"] > 0.01
                    ],
                },
                "energy": {
                    "ev_charger_kwh": round(ev_kwh, 3),
                    "site_import_kwh": round(site_import_kwh, 3),
                    "estimated_solar_assist_kwh": round(solar_assist_kwh, 3),
                    "site_production_kwh": round(production["kwh"], 3),
                    "site_export_kwh": round(exported["kwh"], 3),
                    "grid_fraction_of_ev_energy": round(grid_fraction, 3),
                },
                "cost": {
                    "import_rate_aud_per_kwh": IMPORT_RATE_AUD_PER_KWH,
                    "feed_in_rate_aud_per_kwh": FEED_IN_RATE_AUD_PER_KWH,
                    "actual_import_cost_aud": round(actual_import_cost, 2),
                    "pure_grid_cost_aud": round(pure_grid_cost, 2),
                    "foregone_feed_in_credit_aud": round(foregone_feed_in, 2),
                    "net_saving_vs_pure_grid_aud": round(net_saving_vs_grid, 2),
                    "effective_cost_aud_per_ev_kwh": round((actual_import_cost + foregone_feed_in) / ev_kwh, 3) if ev_kwh > 0 else 0.0,
                },
                "meter_delta_times": {
                    "production": {"first": production["first"], "last": production["last"]},
                    "import": {"first": imported["first"], "last": imported["last"]},
                    "export": {"first": exported["first"], "last": exported["last"]},
                },
                "notes": [
                    "Solar assist is estimated as EV charger kWh minus whole-site grid import kWh during the same window.",
                    "This slightly overstates EV solar assist if non-EV household loads were also using grid import, but is a useful household-level cost comparison.",
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
