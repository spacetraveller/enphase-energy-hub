import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import (  # noqa: E402
    DEFAULT_TIMEZONE,
    ENERGY_FLOW_MEASUREMENT,
    METER_READING_MEASUREMENT,
    env_timezone,
    flux_time,
    parse_hhmm as parse_local_time,
    query_measurement_rows,
    query_measurement_values,
    meter_eid_for_role,
)
from record.household_config import household_ev_enabled  # noqa: E402


@dataclass(frozen=True)
class SeriesSpec:
    key: str
    label: str
    measurement: str
    field: str
    filters: tuple[str, ...] = ()


def series_specs() -> list[SeriesSpec]:
    production_eid = meter_eid_for_role("production")
    net_eid = meter_eid_for_role("net")
    return [
        SeriesSpec(
            "produced",
            "Produced",
            METER_READING_MEASUREMENT,
            "act_energy_dlvd",
            (f'r.eid == "{production_eid}"',),
        ),
        SeriesSpec(
            "consumed",
            "Consumed",
            ENERGY_FLOW_MEASUREMENT,
            "wh_lifetime",
            ('r.section == "consumption"', 'r.measurement_type == "total-consumption"'),
        ),
        SeriesSpec(
            "exported",
            "Exported",
            METER_READING_MEASUREMENT,
            "act_energy_rcvd",
            (f'r.eid == "{net_eid}"',),
        ),
        SeriesSpec(
            "imported",
            "Imported",
            METER_READING_MEASUREMENT,
            "act_energy_dlvd",
            (f'r.eid == "{net_eid}"',),
        ),
    ]


def cumulative_points(
    spec: SeriesSpec, start: datetime, stop: datetime
) -> list[tuple[datetime, float]]:
    return [
        (row.time, row.value)
        for row in query_measurement_values(
            spec.measurement,
            field=spec.field,
            start=flux_time(start),
            stop=flux_time(stop),
            filters=spec.filters,
        )
    ]


def point_deltas_by_hour(
    points: list[tuple[datetime, float]], local_tz: ZoneInfo
) -> tuple[float, dict[int, float]]:
    if len(points) < 2:
        return 0.0, {}
    hourly = defaultdict(float)
    total = 0.0
    previous_time, previous_value = points[0]
    for current_time, current_value in points[1:]:
        delta_wh = current_value - previous_value
        if delta_wh >= 0:
            kwh = delta_wh / 1000.0
            total += kwh
            hourly[current_time.astimezone(local_tz).hour] += kwh
        previous_time = current_time
        previous_value = current_value
    return total, dict(hourly)


def w_now_points(
    start: datetime, stop: datetime, local_tz: ZoneInfo
) -> list[tuple[datetime, float]]:
    return [
        (row.time.astimezone(local_tz), row.value)
        for row in query_measurement_values(
            ENERGY_FLOW_MEASUREMENT,
            field="w_now",
            start=flux_time(start),
            stop=flux_time(stop),
            filters=('r.section == "consumption"', 'r.measurement_type == "total-consumption"'),
        )
    ]


def high_load_segments(
    points: list[tuple[datetime, float]], threshold_w: float, min_minutes: float
) -> list[dict]:
    segments = []
    active = []
    for point in points:
        if point[1] >= threshold_w:
            active.append(point)
            continue
        if active:
            append_segment(segments, active, min_minutes)
            active = []
    if active:
        append_segment(segments, active, min_minutes)
    return segments


def minutes_from_midnight(value: time) -> int:
    return value.hour * 60 + value.minute


def time_distance_minutes(left: time, right: time) -> int:
    left_minutes = minutes_from_midnight(left)
    right_minutes = minutes_from_midnight(right)
    distance = abs(left_minutes - right_minutes)
    return min(distance, 24 * 60 - distance)


def classify_segment(segment: dict, signatures: dict) -> dict:
    start = datetime.fromisoformat(str(segment["start"]))
    duration = float(segment["duration_minutes"])
    avg_kw = float(segment["avg_kw"])
    max_kw = float(segment["max_kw"])
    hot_water = signatures.get("hot_water", {})
    ev_charging = signatures.get("ev_charging", {})

    hot_score = 0
    hot_reasons = []
    hot_start = hot_water.get("typical_start_local")
    if isinstance(hot_start, str):
        tolerance = int(hot_water.get("start_tolerance_minutes", 45))
        if time_distance_minutes(start.timetz().replace(tzinfo=None), parse_local_time(hot_start)) <= tolerance:
            hot_score += 2
            hot_reasons.append("starts near hot water timer")
    hot_duration = hot_water.get("observed_duration_minutes", {})
    if hot_duration.get("min", 0) <= duration <= hot_duration.get("max", 10**9):
        hot_score += 1
        hot_reasons.append("duration matches observed hot water range")
    hot_power = hot_water.get("power_kw", {})
    if hot_power.get("min", 0) <= avg_kw <= hot_power.get("max", 10**9):
        hot_score += 1
        hot_reasons.append("average load matches hot water power range")

    # Whole-site load shape is only circumstantial evidence for EV charging.
    # In a household configured without an EV, leave an unmatched high load
    # unknown instead of applying another household's EV signature.
    if not household_ev_enabled():
        if hot_score >= 3:
            return {"label": "likely_hot_water", "score": hot_score, "reasons": hot_reasons}
        return {"label": "unknown_high_load", "score": hot_score, "reasons": hot_reasons}

    ev_score = 0
    ev_reasons = []
    ev_duration = ev_charging.get("sustained_duration_minutes", {})
    if duration >= ev_duration.get("min", 30):
        ev_score += 1
        ev_reasons.append("sustained load long enough for charging")
    ev_power = ev_charging.get("power_kw", {})
    high_min = ev_power.get("high_rate_typical_min", 5.0)
    high_max = ev_power.get("observed_max", 8.0)
    if avg_kw >= high_min or max_kw >= high_min:
        ev_score += 2
        ev_reasons.append("power matches high-rate EV charging")
    if avg_kw <= high_max and max_kw <= high_max + 1.0:
        ev_score += 1
        ev_reasons.append("peak is within observed EV charging range")
    if hot_score < 3:
        ev_score += 1
        ev_reasons.append("does not strongly match the hot water timer window")

    if hot_score >= ev_score and hot_score >= 3:
        return {"label": "likely_hot_water", "score": hot_score, "reasons": hot_reasons}
    if ev_score >= 3:
        return {"label": "likely_ev_charging", "confidence": "inferred",
                "basis": "whole_site_load_shape", "score": ev_score, "reasons": ev_reasons}
    return {"label": "unknown_high_load", "score": max(hot_score, ev_score), "reasons": hot_reasons + ev_reasons}


def load_signatures() -> dict:
    root = Path(__file__).resolve().parents[2]
    path = root / "record" / "knowledge" / "household_load_signatures.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle).get("signatures", {})


def append_segment(
    segments: list[dict], active: list[tuple[datetime, float]], min_minutes: float
) -> None:
    start = active[0][0]
    stop = active[-1][0]
    duration_minutes = (stop - start).total_seconds() / 60.0
    if duration_minutes < min_minutes:
        return
    values = [value for _, value in active]
    segments.append(
        {
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "duration_minutes": round(duration_minutes, 1),
            "avg_kw": round(sum(values) / len(values) / 1000.0, 2),
            "max_kw": round(max(values) / 1000.0, 2),
        }
    )


def ev_intervals(
    start: datetime, stop: datetime, local_tz: ZoneInfo
) -> list[dict]:
    intervals = []
    for row in query_measurement_rows(
        "enphase_ev_charger_telemetry",
        start=flux_time(start),
        stop=flux_time(stop),
        fields=["consumption_wh"],
        keep_columns=["_time", "_value", "serial_no"],
    ):
        timestamp = row["_time"].astimezone(local_tz)
        wh = float(row.get("_value") or 0.0)
        if wh <= 0:
            continue
        intervals.append(
            {
                "start": timestamp.isoformat(),
                "kwh": round(wh / 1000.0, 3),
                "kw": round(wh / 250.0, 2),
            }
        )
    return intervals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize a local day's production, consumption, grid flow, and high-load periods."
    )
    parser.add_argument("--date", default="")
    parser.add_argument("--timezone", default=env_timezone("HISTORY_TIMEZONE", default=DEFAULT_TIMEZONE))
    parser.add_argument("--high-load-threshold-w", type=float, default=1800.0)
    parser.add_argument("--min-high-load-minutes", type=float, default=10.0)
    args = parser.parse_args()

    local_tz = ZoneInfo(args.timezone)
    today = datetime.now(local_tz).date()
    target_date = date.fromisoformat(args.date) if args.date else today
    start = datetime.combine(target_date, time.min, tzinfo=local_tz)
    stop = min(start + timedelta(days=1), datetime.now(local_tz))

    signatures = load_signatures()

    summary = {
        "date": target_date.isoformat(),
        "timezone": args.timezone,
        "range": {"start": start.isoformat(), "stop": stop.isoformat()},
        "metrics": {},
        "hourly_kwh": {},
        "high_load_segments": [],
        "ev_charger": {"total_kwh": 0.0, "intervals": []},
    }

    for spec in series_specs():
        points = cumulative_points(spec, start, stop)
        total, hourly = point_deltas_by_hour(points, local_tz)
        first = points[0][0].astimezone(local_tz).isoformat() if points else None
        last = points[-1][0].astimezone(local_tz).isoformat() if points else None
        summary["metrics"][spec.key] = {
            "label": spec.label,
            "kwh": round(total, 3),
            "points": len(points),
            "first": first,
            "last": last,
        }
        for hour, kwh in hourly.items():
            summary["hourly_kwh"].setdefault(f"{hour:02d}:00", {})[spec.key] = round(kwh, 3)

    consumption_points = w_now_points(start, stop, local_tz)
    segments = high_load_segments(
        consumption_points, args.high_load_threshold_w, args.min_high_load_minutes
    )
    for segment in segments:
        if signatures:
            segment["classification"] = classify_segment(segment, signatures)
    summary["high_load_segments"] = segments

    intervals = ev_intervals(start, stop, local_tz)
    summary["ev_charger"] = {
        "total_kwh": round(sum(row["kwh"] for row in intervals), 3),
        "intervals": intervals,
    }

    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
