import argparse
import json
import os
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient


def flux_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def query_points(client: InfluxDBClient, bucket: str, start: datetime, stop: datetime, tz: ZoneInfo) -> list[dict]:
    query = f'''
from(bucket: "{bucket}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "enphase_energy_flow")
  |> filter(fn: (r) => r._field == "w_now")
  |> filter(fn: (r) => r.section == "consumption" or r.section == "production")
  |> filter(fn: (r) => r.measurement_type == "total-consumption" or r.measurement_type == "production")
  |> keep(columns: ["_time", "_value", "section", "measurement_type"])
  |> group()
  |> sort(columns: ["_time"])
'''
    by_time = {}
    for table in client.query_api().query(query):
        for record in table.records:
            timestamp = record.get_time().astimezone(tz)
            row = by_time.setdefault(timestamp, {"time": timestamp, "consumption_w": None, "production_w": None})
            section = record.values.get("section")
            measurement_type = record.values.get("measurement_type")
            value = float(record.get_value())
            if section == "consumption" and measurement_type == "total-consumption":
                row["consumption_w"] = value
            elif section == "production":
                row["production_w"] = value
    return [by_time[key] for key in sorted(by_time)]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def contiguous_segments(points: list[dict], baseline_w: float, min_rise_w: float) -> list[list[dict]]:
    segments = []
    active = []
    for point in points:
        value = point.get("consumption_w")
        is_high = value is not None and value >= baseline_w + min_rise_w
        if is_high:
            active.append(point)
            continue
        if active:
            segments.append(active)
            active = []
    if active:
        segments.append(active)
    return segments


def summarize_segment(segment: list[dict], baseline_w: float) -> dict:
    values = [point["consumption_w"] for point in segment if point.get("consumption_w") is not None]
    start = segment[0]["time"]
    stop = segment[-1]["time"]
    duration = (stop - start).total_seconds() / 60.0
    sample_gap_minutes = None
    if len(segment) > 1:
        gaps = [
            (right["time"] - left["time"]).total_seconds() / 60.0
            for left, right in zip(segment, segment[1:])
        ]
        sample_gap_minutes = median(gaps)
    avg_w = sum(values) / len(values)
    excess_w = [max(0.0, value - baseline_w) for value in values]
    gap_hours = (sample_gap_minutes or 0.5) / 60.0
    estimated_wh = sum(excess_w) * gap_hours
    return {
        "start_local": start.isoformat(),
        "stop_local": stop.isoformat(),
        "duration_minutes_between_first_last_samples": round(duration, 2),
        "samples": len(segment),
        "sample_gap_minutes": round(sample_gap_minutes, 2) if sample_gap_minutes else None,
        "baseline_w": round(baseline_w, 1),
        "avg_w": round(avg_w, 1),
        "max_w": round(max(values), 1),
        "avg_rise_w": round(avg_w - baseline_w, 1),
        "max_rise_w": round(max(values) - baseline_w, 1),
        "estimated_incremental_wh": round(estimated_wh, 1),
    }


def rolling_transitions(points: list[dict], window_samples: int, minimum_change_w: float) -> list[dict]:
    consumption_points = [
        point for point in points if point.get("consumption_w") is not None
    ]
    transitions = []
    if len(consumption_points) < window_samples * 2:
        return transitions
    for index in range(window_samples, len(consumption_points) - window_samples + 1):
        before = consumption_points[index - window_samples : index]
        after = consumption_points[index : index + window_samples]
        before_values = [point["consumption_w"] for point in before]
        after_values = [point["consumption_w"] for point in after]
        before_avg = sum(before_values) / len(before_values)
        after_avg = sum(after_values) / len(after_values)
        change_w = after_avg - before_avg
        if abs(change_w) < minimum_change_w:
            continue
        transitions.append(
            {
                "time_local": consumption_points[index]["time"].isoformat(),
                "direction": "up" if change_w > 0 else "down",
                "before_avg_w": round(before_avg, 1),
                "after_avg_w": round(after_avg, 1),
                "change_w": round(change_w, 1),
                "before_min_w": round(min(before_values), 1),
                "before_max_w": round(max(before_values), 1),
                "after_min_w": round(min(after_values), 1),
                "after_max_w": round(max(after_values), 1),
                "window_samples": window_samples,
            }
        )
    return sorted(transitions, key=lambda row: abs(row["change_w"]), reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze a recent short appliance load from Energy Flow samples.")
    parser.add_argument("--lookback-minutes", type=float, default=30.0)
    parser.add_argument("--baseline-minutes", type=float, default=10.0)
    parser.add_argument("--min-rise-w", type=float, default=500.0)
    parser.add_argument("--transition-samples", type=int, default=3)
    parser.add_argument("--min-transition-w", type=float, default=200.0)
    parser.add_argument("--timezone", default=os.getenv("HISTORY_TIMEZONE", "UTC"))
    args = parser.parse_args()

    tz = ZoneInfo(args.timezone)
    stop = datetime.now(tz)
    start = stop - timedelta(minutes=args.lookback_minutes)
    baseline_stop = start + timedelta(minutes=args.baseline_minutes)

    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )
    try:
        points = query_points(client, os.environ["INFLUXDB_BUCKET"], start, stop, tz)
    finally:
        client.close()

    consumption_values = [point["consumption_w"] for point in points if point.get("consumption_w") is not None]
    baseline_values = [
        point["consumption_w"]
        for point in points
        if point.get("consumption_w") is not None and point["time"] <= baseline_stop
    ]
    baseline_w = median(baseline_values or consumption_values or [0.0])
    segments = [summarize_segment(segment, baseline_w) for segment in contiguous_segments(points, baseline_w, args.min_rise_w)]
    transitions = rolling_transitions(points, args.transition_samples, args.min_transition_w)

    result = {
        "timezone": args.timezone,
        "range": {"start": start.isoformat(), "stop": stop.isoformat()},
        "baseline_window_stop": baseline_stop.isoformat(),
        "point_count": len(points),
        "consumption_w": {
            "min": round(min(consumption_values), 1) if consumption_values else None,
            "median": round(median(consumption_values), 1) if consumption_values else None,
            "p90": round(percentile(consumption_values, 0.9), 1) if consumption_values else None,
            "max": round(max(consumption_values), 1) if consumption_values else None,
        },
        "detected_segments": segments,
        "largest_transitions": transitions[:10],
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
