import json
import math
import os
import argparse
import sys
import requests
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.query_helpers import query_history_records, complete_interval_days, env_timezone
from record.history import forecast_model as model
from record.household_config import household_ev_enabled, ev_history_start_date, green_charge_surplus_kw
from record.history.forecast_data import (load_weather, load_ev_days, historical_radiation,
                                          archive_forecasts, mark_published, issued_residuals, load_household_calendar)


LOCAL_TZ = ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
MEASUREMENT = "enphase_history_15m"
FIELDS = {
    "energy_produced_wh": "Produced",
    "energy_consumed_wh": "Consumed",
    "exported_to_grid_wh": "Exported",
    "imported_from_grid_wh": "Imported",
}
EXPECTED_GTI_KWH_M2 = {
    1: 6.0,
    2: 5.6,
    3: 5.0,
    4: 4.4,
    5: 3.9,
    6: 3.5,
    7: 3.8,
    8: 4.5,
    9: 5.2,
    10: 5.8,
    11: 6.1,
    12: 6.2,
}


@dataclass
class Forecast:
    likely: float
    low: float
    high: float
    sample_days: int


def circular_day_distance(left: int, right: int) -> int:
    diff = abs(left - right)
    return min(diff, 366 - diff)


def weighted_quantile(values: list[tuple[float, float]], quantile: float) -> float:
    ordered = sorted((value, weight) for value, weight in values if weight > 0)
    if not ordered:
        return 0.0
    total_weight = sum(weight for _, weight in ordered)
    threshold = total_weight * quantile
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= threshold:
            return value
    return ordered[-1][0]


def weighted_mean(values: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in values if weight > 0)
    if total_weight <= 0:
        return 0.0
    return sum(value * weight for value, weight in values if weight > 0) / total_weight


def fetch_history() -> list[tuple[datetime, str, float]]:
    return query_history_records(local_tz=LOCAL_TZ)


def influx_client() -> InfluxDBClient:
    return InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )


class ForecastHistory(dict):
    """Daily mapping with complete interval inputs for existing diagnostic callers."""


def build_daily_and_hourly(
    records: list[tuple[datetime, str, float]],
) -> tuple[dict[date, dict[str, float]], dict[date, dict[str, dict[int, float]]]]:
    slots, quality = complete_interval_days(records, fields=FIELDS,
                                           before=datetime.now(LOCAL_TZ).date())
    daily_wh = ForecastHistory()
    daily_wh.slots = slots
    daily_wh.quality = quality
    hourly_wh: dict[date, dict[str, dict[int, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )
    for day, values in slots.items():
        daily_wh[day] = {field: sum(series) for field, series in values.items()}
        for field, series in values.items():
            hourly_wh[day][field] = {hour: sum(series[hour * 4:hour * 4 + 4]) for hour in range(24)}
    return daily_wh, hourly_wh


def day_weight(target: date, candidate: date, latest: date) -> float:
    target_doy = target.timetuple().tm_yday
    candidate_doy = candidate.timetuple().tm_yday
    doy_distance = circular_day_distance(target_doy, candidate_doy)
    seasonal_weight = math.exp(-((doy_distance / 35.0) ** 2))
    month_weight = 0.45 if candidate.month == target.month else 0.0
    weekday_weight = 0.18 if candidate.weekday() == target.weekday() else 0.0
    days_from_latest = max((latest - candidate).days, 0)
    recency_weight = 0.35 * math.exp(-(days_from_latest / 120.0))
    return seasonal_weight + month_weight + weekday_weight + recency_weight


def forecast_daily(
    target: date, daily_wh: dict[date, dict[str, float]]
) -> dict[str, Forecast]:
    # Legacy historical baseline retained for comparison reports only.
    daily_wh = {day: values for day, values in daily_wh.items() if day < target}
    latest = max(daily_wh)
    forecasts: dict[str, Forecast] = {}
    for field, label in FIELDS.items():
        samples = [
            (metrics[field] / 1000.0, day_weight(target, sample_date, latest))
            for sample_date, metrics in daily_wh.items()
            if field in metrics and metrics[field] >= 0
        ]
        forecasts[label] = Forecast(
            likely=weighted_mean(samples),
            low=weighted_quantile(samples, 0.2),
            high=weighted_quantile(samples, 0.8),
            sample_days=len(samples),
        )
    return forecasts


def build_forecast_for_target(
    target: date,
    daily_wh: dict[date, dict[str, float]],
    hourly_wh: dict[date, dict[str, dict[int, float]]],
) -> dict[str, object]:
    if not isinstance(daily_wh, ForecastHistory):
        raise ValueError("Use build_daily_and_hourly to retain validated quarter-hour data")
    cutoff = min(target, datetime.now(LOCAL_TZ).date())
    if not hasattr(daily_wh, "ev_days"):
        daily_wh.ev_days = load_ev_days(LOCAL_TZ, datetime.now(LOCAL_TZ).date())
        daily_wh.ev_enabled = household_ev_enabled()
        daily_wh.ev_history_start = ev_history_start_date()
        daily_wh.radiation_history = historical_radiation(LOCAL_TZ)
        daily_wh.residual_cache = {}
        daily_wh.excluded_demand_days, daily_wh.household_calendar = load_household_calendar()
    radiation, weather_summary, payload = load_weather(target, LOCAL_TZ)
    enhanced = model.predict(target, daily_wh.slots, daily_wh.ev_days, radiation, daily_wh.radiation_history,
                             excluded_demand_days=daily_wh.excluded_demand_days,
                             ev_enabled=daily_wh.ev_enabled, regime_start=daily_wh.ev_history_start)
    historical = model.predict(target, daily_wh.slots, daily_wh.ev_days,
                               excluded_demand_days=daily_wh.excluded_demand_days,
                               ev_enabled=daily_wh.ev_enabled, regime_start=daily_wh.ev_history_start)
    selected = enhanced["scenarios"]["expected"]
    baseline = historical["scenarios"]["expected"]
    if cutoff not in daily_wh.residual_cache:
        daily_wh.residual_cache[cutoff] = model.rolling_residuals(
            daily_wh.slots, daily_wh.ev_days, daily_wh.radiation_history, before=cutoff,
            excluded_demand_days=daily_wh.excluded_demand_days,
            ev_enabled=daily_wh.ev_enabled, regime_start=daily_wh.ev_history_start)
    residuals = daily_wh.residual_cache[cutoff] if enhanced["solar_fit"] else []
    lead = (target - datetime.now(LOCAL_TZ).date()).days
    genuine_residuals = issued_residuals(daily_wh.slots, before=cutoff, lead=lead,
                                        version=model.VERSION, local_tz=LOCAL_TZ,
                                        excluded_demand_days=daily_wh.excluded_demand_days)
    lead_calibrated = len(genuine_residuals) >= 14 and bool(enhanced["solar_fit"])
    if lead_calibrated:
        residuals = genuine_residuals
    def daily_values(result, errors):
        output = {}
        for metric, likely in result["daily"].items():
            bounds = model.residual_bounds(likely, metric, errors, result["load_spread"][metric])
            # Historical solar uncertainty is the seasonal analogue distribution.
            if not errors and metric == "Produced":
                samples = [(sum(values["energy_produced_wh"]) / 1000.0, model.solar_weight(target, day))
                           for day, values in daily_wh.slots.items() if day < target]
                bounds = {"low": min(likely, model.quantile(samples, .2)),
                          "high": max(likely, model.quantile(samples, .8))}
            output[metric] = Forecast(likely, bounds["low"], bounds["high"], result["sample_days"])
        return output
    if weather_summary["available"]:
        daylight = [hour for hour, value in enumerate(radiation) if value > 20]
        hourly = payload["hourly"]
        indices = {raw: i for i, raw in enumerate(hourly["time"])}
        def weather_average(field):
            values = []
            for hour in daylight:
                key = f"{target.isoformat()}T{hour:02d}:00"
                index = indices.get(key)
                series = hourly.get(field, [])
                if index is not None and index < len(series) and series[index] is not None:
                    values.append(float(series[index]))
            return sum(values) / len(values) if values else 0.0
        total = sum(radiation) / 1000.0
        expected = EXPECTED_GTI_KWH_M2[target.month]
        weather_summary.update({"daylight_hours": len(daylight),
            "total_global_tilted_irradiance_kwh_m2": total,
            "expected_month_global_tilted_irradiance_kwh_m2": expected,
            "irradiance_ratio": total / expected,
            "avg_daylight_cloud_cover_percent": weather_average("cloud_cover"),
            "avg_daylight_precipitation_probability_percent": weather_average("precipitation_probability"),
            "production_multiplier": selected["daily"]["Produced"] / baseline["daily"]["Produced"]
                                     if baseline["daily"]["Produced"] else 0.0,
            "radiation_basis": "horizontal_GHI; legacy GTI field name retained for dashboard compatibility",
            "cloud_rain_penalty": False})
    return {
        "target_date": target.isoformat(),
        "weather": weather_summary,
        "daily_kwh": daily_values(baseline, []),
        "weather_adjusted_daily_kwh": daily_values(selected, residuals),
        "hourly_kwh": baseline["hourly"],
        "weather_adjusted_hourly_kwh": selected["hourly"],
        "quarter_hourly_kwh": selected["slots"],
        "scenarios": enhanced["scenarios"],
        "diagnostics": {key: value for key, value in enhanced.items() if key != "scenarios"},
        "quality": daily_wh.quality,
        "household_calendar": daily_wh.household_calendar,
        "uncertainty": {"method": "issued lead-time p20-p80 residuals" if lead_calibrated else "rolling retrospective p20-p80 residuals" if len(residuals) >= 14 else "historical analogue spread",
                        "residual_days": len(residuals), "nominal_coverage": 0.6,
                        "lead_time_calibrated": lead_calibrated, "issued_residual_days": len(genuine_residuals),
                        "note": "Marginal bounds based on earlier issued forecasts at this lead." if lead_calibrated else "Marginal bounds; reconstructed weather understates future weather uncertainty. Issuance archive enables lead-time evaluation."},
    }


def forecast_as_json(
    forecast: dict[str, object],
) -> dict[str, object]:
    def daily_json(values: dict[str, Forecast]) -> dict[str, dict[str, float | int]]:
        return {
            label: {
                "likely": item.likely,
                "low": item.low,
                "high": item.high,
                "sample_days": item.sample_days,
            }
            for label, item in values.items()
        }

    return {
        **forecast,
        "daily_kwh": daily_json(forecast["daily_kwh"]),  # type: ignore[arg-type]
        "weather_adjusted_daily_kwh": daily_json(
            forecast["weather_adjusted_daily_kwh"]  # type: ignore[arg-type]
        ),
        "hourly_kwh": forecast["hourly_kwh"],
        "weather_adjusted_hourly_kwh": forecast["weather_adjusted_hourly_kwh"],
    }


def write_forecasts(forecasts: list[dict[str, object]]) -> int:
    org = os.environ["INFLUXDB_ORG"]
    bucket = os.environ["INFLUXDB_BUCKET"]
    fetched_at = datetime.now(LOCAL_TZ)
    points: list[Point] = []

    for forecast in forecasts:
        target = date.fromisoformat(str(forecast["target_date"]))
        daily_time = datetime(
            target.year, target.month, target.day, 12, 0, tzinfo=LOCAL_TZ
        )
        for model_name, key in (
            ("historical", "daily_kwh"),
            ("weather_adjusted", "weather_adjusted_daily_kwh"),
        ):
            daily = forecast[key]
            for metric, values in daily.items():  # type: ignore[union-attr]
                points.append(
                    Point("enphase_prediction_daily")
                    .tag("model", model_name)
                    .tag("metric", metric)
                    .tag("target_date", target.isoformat())
                    .field("likely_kwh", float(values.likely))
                    .field("low_kwh", float(values.low))
                    .field("high_kwh", float(values.high))
                    .field("sample_days", int(values.sample_days))
                    .field("fetched_at_unix", int(fetched_at.timestamp()))
                    .field("model_version", model.VERSION)
                    .field("weather_available", bool(forecast["weather"].get("available")))
                    .field("lead_time_calibrated", bool(forecast["uncertainty"]["lead_time_calibrated"]))
                    .time(daily_time, WritePrecision.NS)
                )

        for model_name, key in (
            ("historical", "hourly_kwh"),
            ("weather_adjusted", "weather_adjusted_hourly_kwh"),
        ):
            hourly = forecast[key]
            for row in hourly:  # type: ignore[union-attr]
                hour = int(str(row["Hour"]).split(":", 1)[0])
                timestamp = datetime(
                    target.year, target.month, target.day, hour, 0, tzinfo=LOCAL_TZ
                )
                for metric in ("Produced", "Consumed", "Exported", "Imported"):
                    point = (
                        Point("enphase_prediction_hourly")
                        .tag("model", model_name)
                        .tag("metric", metric)
                        .tag("target_date", target.isoformat())
                        .field("kwh", float(row[metric]))
                        .field("fetched_at_unix", int(fetched_at.timestamp()))
                        .time(timestamp, WritePrecision.NS)
                    )
                    if (model_name == "weather_adjusted"
                            and metric == "Consumed"):
                        scenario = forecast.get("scenarios", {}).get("no_ev")
                        valid = bool(household_ev_enabled() and scenario and forecast["weather"].get("available")
                                     and forecast.get("diagnostics", {}).get("solar_fit")
                                     and forecast.get("diagnostics", {}).get("load_model", {}).get("ev_separated"))
                        slots = scenario["slots"][hour * 4:hour * 4 + 4] if valid else []
                        surplus = [max(0.0, slot["Produced"] - slot["Consumed"]) * 4 for slot in slots]
                        threshold = green_charge_surplus_kw()
                        quarters = sum(value >= threshold for value in surplus)
                        issued = forecast["weather"].get("issued_at")
                        point.field("non_ev_kwh", float(sum(slot["Consumed"] for slot in slots)))
                        point.field("available_ev_kw", float(sum(surplus) / 4 if valid else 0))
                        point.field("minimum_ev_surplus_kw", float(min(surplus) if valid else 0))
                        point.field("green_charge_status", float(2 if quarters == 4 else 1 if quarters else 0) if valid else -1.0)
                        point.field("ev_planning_available", float(valid))
                        point.field("weather_issued_unix", float(datetime.fromisoformat(issued).timestamp()) if issued else 0.0)
                    points.append(point)

        weather = forecast["weather"]
        if isinstance(weather, dict) and weather.get("available"):
            point = (
                Point("enphase_prediction_weather")
                .tag("provider", "open-meteo")
                .tag("target_date", target.isoformat())
                .field("hours", int(weather["hours"]))
                .field("daylight_hours", int(weather["daylight_hours"]))
                .field(
                    "total_global_tilted_irradiance_kwh_m2",
                    float(weather["total_global_tilted_irradiance_kwh_m2"]),
                )
                .field(
                    "expected_month_global_tilted_irradiance_kwh_m2",
                    float(weather["expected_month_global_tilted_irradiance_kwh_m2"]),
                )
                .field("irradiance_ratio", float(weather["irradiance_ratio"]))
                .field(
                    "avg_daylight_cloud_cover_percent",
                    float(weather["avg_daylight_cloud_cover_percent"]),
                )
                .field(
                    "avg_daylight_precipitation_probability_percent",
                    float(weather["avg_daylight_precipitation_probability_percent"]),
                )
                .field("production_multiplier", float(weather["production_multiplier"]))
                .field("fetched_at_unix", int(fetched_at.timestamp()))
                .time(daily_time, WritePrecision.NS)
            )
            points.append(point)

    if not points:
        return 0
    client = influx_client()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    write_api.write(bucket=bucket, org=org, record=points)
    client.close()
    return len(points)


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh and archive consistent household energy forecasts")
    parser.add_argument("--dry-run", action="store_true", help="Compute without publishing predictions or archiving an issuance")
    parser.add_argument("--no-refresh-weather", action="store_true", help="Use validated cached weather inputs")
    parser.add_argument("--full-output", action="store_true")
    parser.add_argument("--allow-historical-fallback", action="store_true", help="Explicitly permit publication without complete fresh weather or solar calibration")
    args = parser.parse_args()
    warnings = []
    if not args.no_refresh_weather:
        from record.weather.fetch_open_meteo import fetch_forecast, write_snapshot, points_from_forecast, write_influx
        payload = fetch_forecast()
        write_snapshot(payload)
        write_influx(points_from_forecast(payload))
        try:
            historical_radiation(LOCAL_TZ, refresh=True)
        except (OSError, ValueError, requests.RequestException) as exc:
            warnings.append(f"Calibration weather refresh failed ({type(exc).__name__}); using cache or historical fallback")
    forecast_days = int(os.getenv("PREDICTION_FORECAST_DAYS", "3"))
    first_offset_days = int(os.getenv("PREDICTION_FIRST_OFFSET_DAYS", "1"))
    first_target = datetime.now(LOCAL_TZ).date() + timedelta(days=first_offset_days)
    records = fetch_history()
    if not records:
        raise RuntimeError("No historical records found in InfluxDB.")

    daily_wh, hourly_wh = build_daily_and_hourly(records)
    if not daily_wh or (datetime.now(LOCAL_TZ).date() - max(daily_wh)).days > 3:
        raise RuntimeError("Household history is incomplete or more than three days stale; import history before publishing")
    forecasts = [
        build_forecast_for_target(first_target + timedelta(days=offset), daily_wh, hourly_wh)
        for offset in range(forecast_days)
    ]
    degraded = [item["target_date"] for item in forecasts if not item["weather"].get("available")
                or not item["diagnostics"]["solar_fit"]]
    if degraded:
        warnings.append("Historical fallback for: " + ", ".join(degraded))
        if not args.dry_run and not args.allow_historical_fallback:
            raise RuntimeError("Refusing to publish degraded forecasts; refresh weather/calibration or explicitly use --allow-historical-fallback")
    output = {
        "issued_at": datetime.now(LOCAL_TZ).isoformat(),
        "model_version": model.VERSION,
        "target_dates": [forecast["target_date"] for forecast in forecasts],
        "timezone": str(LOCAL_TZ),
        "method": "measured hourly solar response, recent household/EV scenarios, 15-minute energy balance",
        "warnings": warnings,
        "history": {
            "first_day": min(daily_wh).isoformat(),
            "last_day": max(daily_wh).isoformat(),
            "days": len(daily_wh),
        },
        "forecasts": [forecast_as_json(forecast) for forecast in forecasts],
    }
    for forecast in output["forecasts"]:
        forecast["lead_days"] = (date.fromisoformat(forecast["target_date"]) - datetime.now(LOCAL_TZ).date()).days
    output["weather_input"] = load_weather(first_target, LOCAL_TZ)[2]
    archive = archive_forecasts(output) if not args.dry_run else None
    count = write_forecasts(forecasts) if not args.dry_run else 0
    if archive:
        mark_published(archive)
    if args.full_output:
        output.update(points_written=count, archive=str(archive) if archive else None)
        print(json.dumps(output, indent=2, allow_nan=False))
    else:
        print(json.dumps({"points_written": count, "archive": str(archive) if archive else None,
                          "model_version": model.VERSION, "history": output["history"], "warnings": warnings,
                          "forecasts": [{"target_date": item["target_date"],
                                         "daily_kwh": item["weather_adjusted_daily_kwh"],
                                         "solar": item["diagnostics"]["solar_method"],
                                         "load": item["diagnostics"]["load_model"],
                                         "weather": item["weather"], "uncertainty": item["uncertainty"]}
                                        for item in output["forecasts"]]}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
