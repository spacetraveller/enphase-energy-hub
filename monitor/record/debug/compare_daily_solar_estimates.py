import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    DEFAULT_TIMEZONE,
    ENERGY_FLOW_MEASUREMENT,
    METER_READING_MEASUREMENT,
    env_timezone,
    first_last_delta,
    flux_time,
    latest_measurement_rows,
    meter_eid_for_role,
)


PRODUCTION_TOTALS_MEASUREMENT = "enphase_production_totals"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare solar production estimates for a local day."
    )
    parser.add_argument("--date", default="")
    parser.add_argument("--timezone", default=env_timezone("DAILY_SOLAR_TIMEZONE", default=DEFAULT_TIMEZONE))
    parser.add_argument(
        "--mode",
        choices=["compare", "dashboard", "latest-sources", "all"],
        default="compare",
    )
    parser.add_argument("--latest-start", default="-4h")
    return parser.parse_args()


def local_range(day: str, timezone_name: str) -> tuple[str, str, str]:
    local_tz = ZoneInfo(timezone_name)
    target = datetime.now(local_tz).date() if not day else datetime.fromisoformat(day).date()
    start_local = datetime.combine(target, time.min, tzinfo=local_tz)
    stop_local = min(start_local.replace(hour=23, minute=59, second=59), datetime.now(local_tz))
    if target < datetime.now(local_tz).date():
        stop_local = datetime.combine(target, time.max, tzinfo=local_tz)
    return target.isoformat(), flux_time(start_local), flux_time(stop_local)


def result_details(result) -> dict:
    return {
        "first_time": result.first_time.isoformat() if result.first_time else None,
        "first": result.first_value,
        "last_time": result.last_time.isoformat() if result.last_time else None,
        "last": result.last_value,
    }


def compare_estimates(start: str, stop: str) -> list[dict]:
    production_meter_eid = meter_eid_for_role("production")
    candidates = [
        (
            "energy_flow_eim_wh_lifetime_delta",
            ENERGY_FLOW_MEASUREMENT,
            "wh_lifetime",
            ['r.section == "production"', 'r.measurement_type == "production"'],
        ),
        (
            "energy_flow_inverters_wh_lifetime_delta",
            ENERGY_FLOW_MEASUREMENT,
            "wh_lifetime",
            ['r.section == "production"', 'r.type == "inverters"'],
        ),
        (
            "meter_all_eids_act_energy_delivered_delta",
            METER_READING_MEASUREMENT,
            "act_energy_dlvd",
            [],
        ),
        (
            "meter_production_act_energy_delivered_delta",
            METER_READING_MEASUREMENT,
            "act_energy_dlvd",
            [f'r.eid == "{production_meter_eid}"'],
        ),
        (
            "production_totals_wh_lifetime_delta",
            PRODUCTION_TOTALS_MEASUREMENT,
            "wh_lifetime",
            [],
        ),
    ]
    rows = []
    for name, measurement, field, filters in candidates:
        delta = first_last_delta(
            measurement,
            field=field,
            start=start,
            stop=stop,
            filters=filters,
        )
        rows.append(
            {
                "name": name,
                "kwh": delta.kwh,
                "details": result_details(delta),
            }
        )
    return rows


def dashboard_value(start: str, stop: str) -> dict:
    production_meter_eid = meter_eid_for_role("production")
    delta = first_last_delta(
        METER_READING_MEASUREMENT,
        field="act_energy_dlvd",
        start=start,
        stop=stop,
        filters=[f'r.eid == "{production_meter_eid}"'],
    )
    return {
        "field": "act_energy_dlvd",
        "eid": production_meter_eid,
        "value_kwh": delta.kwh,
        "details": result_details(delta),
    }


def latest_sources(start: str) -> list[dict]:
    rows = latest_measurement_rows(
        ENERGY_FLOW_MEASUREMENT,
        start=start,
        fields=["w_now", "wh_today", "wh_lifetime"],
        filters=['r.section == "production"', 'r.measurement_type == "production"'],
        group_columns=["_measurement", "_field"],
        keep_columns=["_time", "_measurement", "_field", "_value"],
    )
    rows.extend(
        latest_measurement_rows(
            PRODUCTION_TOTALS_MEASUREMENT,
            start=start,
            fields=[
                "watt_hours_today",
                "watt_hours_lifetime",
                "watts_now",
                "wh_today",
                "wh_lifetime",
                "w_now",
            ],
            group_columns=["_measurement", "_field"],
            keep_columns=["_time", "_measurement", "_field", "_value"],
        )
    )
    return [
        {
            "time": row.get("_time").isoformat() if row.get("_time") else None,
            "measurement": row.get("_measurement"),
            "field": row.get("_field"),
            "value": row.get("_value"),
        }
        for row in rows
    ]


def main() -> None:
    args = parse_args()
    target, start, stop = local_range(args.date, args.timezone)
    output = {
        "local_day": target,
        "timezone": args.timezone,
        "range_utc": {"start": start, "stop": stop},
    }

    if args.mode in {"compare", "all"}:
        output["estimates"] = compare_estimates(start, stop)
    if args.mode in {"dashboard", "all"}:
        output["dashboard_solar_today"] = dashboard_value(start, stop)
    if args.mode in {"latest-sources", "all"}:
        output["latest_sources"] = latest_sources(args.latest_start)

    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
