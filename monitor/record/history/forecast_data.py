"""Forecast input validation, cached weather calibration, and issuance archives."""
import hashlib
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

from record.query_helpers import complete_interval_days, query_measurement_rows
from record.household_config import ev_charger_telemetry_enabled, ev_history_start_date

ROOT = Path(__file__).resolve().parent


def load_household_calendar(path=None):
    """Read user-confirmed exceptions; keep the actual readings untouched."""
    if path is None:
        configured_path = os.getenv("HOUSEHOLD_CALENDAR_PATH", "").strip()
        path = Path(configured_path) if configured_path else ROOT.parent / "knowledge" / "household_calendar.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set(), {"periods": []}
    excluded = set()
    for period in payload["periods"]:
        start = date.fromisoformat(period["start_date"])
        end = date.fromisoformat(period["end_date"])
        if end < start:
            raise ValueError("Household calendar end_date precedes start_date")
        if period.get("exclude_from_normal_demand"):
            excluded.update(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    return excluded, payload


def radiation_days(payload, local_tz):
    """Open-Meteo radiation at 11:00 describes 10:00-11:00, including midnight."""
    hourly = payload.get("hourly", {})
    grouped = {}
    values = hourly.get("shortwave_radiation", [])
    for index, raw in enumerate(hourly.get("time", [])):
        if index >= len(values) or values[index] is None:
            continue
        value = float(values[index])
        if not math.isfinite(value) or value < 0:
            continue
        timestamp = datetime.fromisoformat(raw)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo(payload.get("timezone", str(local_tz))))
        start = timestamp.astimezone(local_tz) - timedelta(hours=1)
        if start.minute or start.second:
            continue
        grouped.setdefault(start.date(), {})[start.hour] = value
    return {day: [values[hour] for hour in range(24)] for day, values in grouped.items()
            if set(values) == set(range(24))}


def load_weather(target, local_tz, *, now=None, path=None):
    now = now or datetime.now(timezone.utc)
    path = path or Path(os.getenv("WEATHER_SNAPSHOT_DIR", "/app/record/weather/snapshots")) / "latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        issued = datetime.fromisoformat(payload["_retrieved_at"])
        age = (now - issued).total_seconds() / 3600.0
        max_age = float(os.getenv("PREDICTION_WEATHER_MAX_AGE_HOURS", "6"))
        if age < -0.1 or age > max_age:
            return None, {"available": False, "reason": "weather_stale", "age_hours": round(age, 2)}, payload
        radiation = radiation_days(payload, local_tz).get(target)
        if radiation is None:
            return None, {"available": False, "reason": "incomplete_weather_day"}, payload
        return radiation, {"available": True, "hours": 24, "age_hours": round(age, 2),
                           "issued_at": issued.isoformat(), "alignment": "preceding_hour_to_interval_start"}, payload
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, {"available": False, "reason": "missing_or_invalid_weather_snapshot",
                      "error_type": type(exc).__name__}, {}


def load_ev_days(local_tz, before):
    if not ev_charger_telemetry_enabled():
        return {}
    regime_start = ev_history_start_date()
    query_start = (f"{regime_start.isoformat()}T00:00:00Z" if regime_start else "-56d")
    rows = query_measurement_rows("enphase_ev_charger_telemetry", start=query_start,
                                  fields=("consumption_wh",),
                                  keep_columns=("_time", "_value", "serial_no"))
    # Validate each charger separately: a missing charger interval is never a zero.
    by_serial = {}
    for row in rows:
        by_serial.setdefault(row.get("serial_no", "unknown"), []).append(
            (row["_time"].astimezone(local_tz), "ev", float(row["_value"])))
    series = [complete_interval_days(records, fields=("ev",), before=before)[0]
              for records in by_serial.values()]
    common = set.intersection(*(set(item) for item in series)) if series else set()
    return {day: [sum(item[day]["ev"][slot] for item in series) for slot in range(96)] for day in common}


def historical_radiation(local_tz, *, refresh=False, now=None):
    """Cache reconstructed weather for response fitting, not lead-time scoring."""
    now = now or datetime.now(local_tz)
    directory = ROOT / "forecast_cache"
    path = directory / "historical_weather.json"
    cached = json.loads(path.read_text()) if path.exists() else {}
    if refresh and cached.get("_cache_date") != now.date().isoformat():
        response = requests.get("https://historical-forecast-api.open-meteo.com/v1/forecast", params={
            "latitude": float(os.environ["WEATHER_LATITUDE"]),
            "longitude": float(os.environ["WEATHER_LONGITUDE"]), "timezone": str(local_tz),
            "start_date": (now.date() - timedelta(days=120)).isoformat(),
            "end_date": (now.date() - timedelta(days=1)).isoformat(),
            "hourly": "shortwave_radiation"}, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if not radiation_days(payload, local_tz):
            raise ValueError("Historical weather response contains no complete days")
        payload["_cache_date"] = now.date().isoformat()
        payload["_retrieved_at"] = now.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
        cached = payload
    return radiation_days(cached, local_tz)


def archive_forecasts(payload, *, directory=None):
    directory = directory or ROOT / "forecast_archive"
    directory.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "_" + uuid4().hex[:8] + ".json"
    path = directory / name
    encoded = json.dumps(payload, indent=2, allow_nan=False)
    # Exclusive create: subsequent runs cannot erase prior issued predictions.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
    return path


def mark_published(path):
    receipt = {"published_at": datetime.now(timezone.utc).isoformat(),
               "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    with path.with_suffix(".published").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream)


def published_forecasts(directory, local_tz):
    """Latest successful issuance per version, target and local-calendar lead day."""
    latest = {}
    for path in sorted(directory.glob("*.json")):
        receipt_path = path.with_suffix(".published")
        if not receipt_path.exists():
            continue
        content = path.read_bytes()
        receipt = json.loads(receipt_path.read_text())
        if hashlib.sha256(content).hexdigest() != receipt["sha256"]:
            raise ValueError(f"Forecast archive integrity check failed: {path.name}")
        payload = json.loads(content)
        issued = datetime.fromisoformat(payload["issued_at"])
        for forecast in payload["forecasts"]:
            target = date.fromisoformat(forecast["target_date"])
            lead = (target - issued.astimezone(local_tz).date()).days
            key = (payload["model_version"], lead, target)
            if key not in latest or issued > latest[key][0]:
                latest[key] = (issued, forecast)
    return latest


def issued_residuals(days, *, before, lead, version, local_tz, count=28, excluded_demand_days=()):
    from record.history.forecast_model import FIELDS
    results = []
    for (candidate_version, candidate_lead, target), (_, forecast) in sorted(
            published_forecasts(ROOT / "forecast_archive", local_tz).items()):
        if (candidate_version != version or candidate_lead != lead or target >= before
                or target not in days or target in excluded_demand_days):
            continue
        results.append({"target_date": target.isoformat(), "residual": {
            metric: sum(days[target][field]) / 1000.0 - forecast["weather_adjusted_daily_kwh"][metric]["likely"]
            for field, metric in FIELDS.items()}})
    return results[-count:]
