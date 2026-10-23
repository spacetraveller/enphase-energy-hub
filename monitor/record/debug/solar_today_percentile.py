import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(Path(__file__).resolve().parents[2]))

from history.predict_tomorrow import (  # noqa: E402
    build_daily_and_hourly,
    build_forecast_for_target,
    fetch_history,
)
from record.query_helpers import percentile_linear as shared_percentile_linear
from record.query_helpers import percentile_rank as shared_percentile_rank
from record.query_helpers import influx_bucket, influx_client, influx_org, meter_eid_for_role, query_rows


LOCAL_TZ = ZoneInfo(os.getenv("HISTORY_TIMEZONE", "UTC"))
MONTHS = {
    int(value.strip())
    for value in os.getenv("SOLAR_PERCENTILE_MONTHS", "4,5").split(",")
    if value.strip()
}
GOOD_DAY_PERCENTILE = float(os.getenv("SOLAR_GOOD_DAY_PERCENTILE", "0.75"))


def percentile_rank(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return shared_percentile_rank(values, value)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    return shared_percentile_linear(values, pct)


def main() -> None:
    production_eid = meter_eid_for_role("production")
    bucket = influx_bucket()
    now = datetime.now(LOCAL_TZ)
    completed_hour = max(now.hour - 1, 0)
    today_start = datetime.combine(now.date(), datetime.min.time(), LOCAL_TZ)
    completed_stop = today_start + timedelta(hours=completed_hour + 1)

    client = influx_client()

    completed_actual_query = f'''
from(bucket: "{bucket}")
  |> range(start: {today_start.isoformat()}, stop: {completed_stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "enphase_meter_reading" and r.eid == "{production_eid}" and r._field == "act_energy_dlvd")
  |> keep(columns: ["_time", "_value"])
  |> group()
'''
    try:
        actual_rows = sorted(
            query_rows(client, completed_actual_query, org=influx_org()),
            key=lambda row: row["_time"],
        )
        if len(actual_rows) >= 2:
            today_completed_kwh = (
                float(actual_rows[-1].get("_value") or 0.0)
                - float(actual_rows[0].get("_value") or 0.0)
            ) / 1000.0
        else:
            today_completed_kwh = 0.0
    finally:
        client.close()

    records = fetch_history()
    by_day: dict[date, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    imported_today_rows = []
    for timestamp, field, value in records:
        if field != "energy_produced_wh":
            continue
        if timestamp.date() == now.date():
            imported_today_rows.append((timestamp, value))
        elif timestamp.month in MONTHS:
            by_day[timestamp.date()][timestamp.hour] += value / 1000.0
    imported_today_kwh = sum(value for _, value in imported_today_rows) / 1000.0

    daily_wh, hourly_wh = build_daily_and_hourly(records)
    forecast = build_forecast_for_target(now.date(), daily_wh, hourly_wh)
    hourly_forecast = forecast["weather_adjusted_hourly_kwh"]
    forecast_full_day = sum(float(row.get("Produced") or 0.0) for row in hourly_forecast)
    forecast_completed = sum(
        float(row.get("Produced") or 0.0)
        for row in hourly_forecast
        if int(str(row["Hour"]).split(":", 1)[0]) <= completed_hour
    )

    full_day_values = [sum(hours.values()) for hours in by_day.values()]
    partial_values = [
        sum(value for hour, value in hours.items() if hour <= completed_hour)
        for hours in by_day.values()
    ]
    good_threshold = percentile(full_day_values, GOOD_DAY_PERCENTILE)
    good_full_days = [value for value in full_day_values if value >= good_threshold]
    good_partial_days = [
        partial
        for full, partial in zip(full_day_values, partial_values, strict=False)
        if full >= good_threshold
    ]

    print(
        json.dumps(
            {
                "now": now.isoformat(),
                "months": sorted(MONTHS),
                "history_days": len(full_day_values),
                "completed_hour": completed_hour,
                "today_completed_kwh": round(today_completed_kwh, 2),
                "today_imported_intervals": len(imported_today_rows),
                "today_imported_full_day_kwh": round(imported_today_kwh, 2),
                "today_forecast_completed_kwh": round(forecast_completed, 2),
                "today_forecast_full_day_kwh": round(forecast_full_day, 2),
                "weather": forecast["weather"],
                "all_days": {
                    "full_day_percentile": round(
                        percentile_rank(full_day_values, forecast_full_day) * 100, 1
                    ),
                    "imported_actual_full_day_percentile": round(
                        percentile_rank(full_day_values, imported_today_kwh) * 100, 1
                    ),
                    "so_far_percentile": round(
                        percentile_rank(partial_values, today_completed_kwh) * 100, 1
                    ),
                    "median_full_day_kwh": round(percentile(full_day_values, 0.5), 2),
                    "p75_full_day_kwh": round(good_threshold, 2),
                    "p90_full_day_kwh": round(percentile(full_day_values, 0.9), 2),
                },
                "good_days": {
                    "definition": f"full-day production >= April/May p{int(GOOD_DAY_PERCENTILE * 100)}",
                    "days": len(good_full_days),
                    "forecast_full_day_percentile": round(
                        percentile_rank(good_full_days, forecast_full_day) * 100, 1
                    ),
                    "so_far_percentile": round(
                        percentile_rank(good_partial_days, today_completed_kwh) * 100, 1
                    ),
                    "median_full_day_kwh": round(percentile(good_full_days, 0.5), 2),
                    "median_so_far_kwh": round(percentile(good_partial_days, 0.5), 2),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
