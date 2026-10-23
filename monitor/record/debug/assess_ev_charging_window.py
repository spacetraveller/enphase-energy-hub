"""Assess green charging using fresh solar and a household load excluding EVs."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.history.predict_tomorrow import LOCAL_TZ, build_daily_and_hourly, build_forecast_for_target, fetch_history
from record.query_helpers import first_last_delta, latest_measurement_rows, meter_eid_for_role
from record.weather.fetch_open_meteo import fetch_forecast, write_snapshot, write_influx, points_from_forecast
from record.household_config import household_ev_enabled, green_charge_surplus_kw


GREEN_CHARGE_KW = green_charge_surplus_kw()


def window_hours(forecasts, start, count=3):
    result = []
    for offset in range(count):
        timestamp = start + timedelta(hours=offset)
        forecast = forecasts[timestamp.date()]
        scenario = forecast["scenarios"].get("no_ev")
        valid = scenario is not None and forecast["weather"].get("available") and forecast["diagnostics"]["solar_fit"]
        if not valid:
            result.append({"time": timestamp.isoformat(), "rating": "unavailable",
                           "reason": "Fresh weather, calibrated solar and complete EV separation are required"})
            continue
        slots = scenario["slots"][timestamp.hour * 4:timestamp.hour * 4 + 4]
        # Net expected production and non-EV demand for a conservative planning
        # signal. This is not a probability of uninterrupted charging.
        surplus = [max(0, row["Produced"] - row["Consumed"]) * 4 for row in slots]
        good = sum(value >= GREEN_CHARGE_KW for value in surplus)
        result.append({"time": timestamp.isoformat(), "rating": "good" if good == 4 else "borderline" if good else "poor",
                       "quarters_above_threshold": good, "minimum_quarter_surplus_kw": round(min(surplus), 2),
                       "average_surplus_kw": round(sum(surplus) / 4, 2),
                       "produced_kwh": round(sum(row["Produced"] for row in slots), 2),
                       "non_ev_consumed_kwh": round(sum(row["Consumed"] for row in slots), 2),
                       "hot_water_estimate_kwh": round(sum(row["HotWaterEstimate"] for row in slots), 2)})
    return result


def main():
    if not household_ev_enabled():
        print(json.dumps({"available": False, "reason": "EV charging is not enabled for this household"}, indent=2))
        return
    payload = fetch_forecast()
    write_snapshot(payload)
    write_influx(points_from_forecast(payload))
    now = datetime.now(LOCAL_TZ)
    start = now.replace(minute=0, second=0, microsecond=0)
    if now > start:
        start += timedelta(hours=1)
    stop = start + timedelta(hours=3)
    daily, hourly = build_daily_and_hourly(fetch_history())
    targets = {(start + timedelta(hours=offset)).date() for offset in range(3)}
    forecasts = {target: build_forecast_for_target(target, daily, hourly) for target in targets}
    hours = window_hours(forecasts, start)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = {}
    production_eid = meter_eid_for_role("production")
    net_eid = meter_eid_for_role("net")
    for metric, eid, field in (("produced_kwh", production_eid, "act_energy_dlvd"),
                               ("exported_kwh", net_eid, "act_energy_rcvd"),
                               ("imported_kwh", net_eid, "act_energy_dlvd")):
        delta = first_last_delta("enphase_meter_reading", field=field, start=midnight.isoformat(),
                                 stop=now.isoformat(), filters=(f'r.eid == "{eid}"',))
        today[metric] = round(delta.kwh, 2) if delta.kwh is not None else None
    live = latest_measurement_rows("enphase_energy_flow", start="-5m", fields=("w_now",),
                                    group_columns=("section", "measurement_type"))
    for row in live:
        kind = (row.get("section"), row.get("measurement_type"))
        name = {("production", "production"): "live_production_kw",
                ("consumption", "total-consumption"): "live_consumption_kw",
                ("consumption", "net-consumption"): "live_net_import_kw"}.get(kind)
        if name:
            today[name] = round(float(row["_value"]) / 1000.0, 2)
    candidates = [row for row in hours if row["rating"] != "unavailable"]
    print(json.dumps({"now": now.isoformat(), "window_start": start.isoformat(), "window_stop": stop.isoformat(),
                      "green_charge_surplus_threshold_kw": GREEN_CHARGE_KW, "today_so_far": today,
                      "hours": hours, "best_hour": max(candidates, key=lambda row: (row["quarters_above_threshold"], row["minimum_quarter_surplus_kw"]), default=None),
                      "note": "Planning estimates, not calibrated charging probabilities. Quarter-hour solar shape is historical; brief clouds or load spikes may interrupt charging."}, indent=2))


if __name__ == "__main__":
    main()
