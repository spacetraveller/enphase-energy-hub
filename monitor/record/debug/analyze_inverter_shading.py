import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.query_helpers import env_timezone, flux_time, parse_hhmm, query_measurement_rows
from record.solar_layout import LayoutError, read_layout


MEASUREMENT = "enphase_inverter_production"
FIELD = "last_report_watts"


def in_window(timestamp: datetime, start: time, stop: time) -> bool:
    return start <= timestamp.timetz().replace(tzinfo=None) < stop


def bucket_time(timestamp: datetime, minutes: int) -> datetime:
    minute = timestamp.minute - timestamp.minute % minutes
    return timestamp.replace(minute=minute, second=0, microsecond=0)


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def load_panel_metadata(map_name: str) -> dict:
    _, layout = read_layout(map_name)
    panel_metadata = {}
    telemetry_ids = set()
    display_fields = []
    for index, panel in enumerate(layout["panels"], start=1):
        telemetry_id = panel.get("telemetry_serial")
        if not telemetry_id:
            raise LayoutError(
                f"panels[{index}].telemetry_serial is required to run the inverter shading report."
            )
        inverter_alias = panel.get("inverter_id")
        if not inverter_alias:
            raise LayoutError(f"panels[{index}].inverter_id is required to run the inverter shading report.")
        telemetry_ids.add(telemetry_id)
        panel_metadata[telemetry_id] = {
            "position_id": panel["position_id"],
            "roof_face": panel["roof_face"],
            "inverter_alias": inverter_alias,
            "channel_alias": panel.get("channel_id"),
        }
        display_fields.extend(
            field
            for field in (
                panel["position_id"],
                panel["roof_face"],
                inverter_alias,
                panel.get("channel_id"),
            )
            if field is not None
        )

    if any(telemetry_id in display_value for telemetry_id in telemetry_ids for display_value in display_fields):
        raise LayoutError("Display aliases must not contain a private telemetry identifier.")
    return panel_metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect time-localized per-inverter shading relative to the simultaneous fleet median."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--baseline-start", default="11:00")
    parser.add_argument("--baseline-stop", default="13:00")
    parser.add_argument("--analysis-start", default="13:00")
    parser.add_argument("--analysis-stop", default="15:30")
    parser.add_argument("--bucket-minutes", type=int, default=5)
    parser.add_argument("--minimum-fleet-median-watts", type=float, default=40.0)
    parser.add_argument(
        "--map",
        default="map.json",
        help="Map filename inside the ignored local solar layout directory.",
    )
    args = parser.parse_args()

    panel_metadata = load_panel_metadata(args.map)

    local_tz = ZoneInfo(env_timezone("ENPHASE_HISTORY_TIMEZONE", "HISTORY_TIMEZONE"))
    target_day = date.fromisoformat(args.date)
    start_dt = datetime.combine(target_day, time.min, tzinfo=local_tz)
    stop_dt = start_dt + timedelta(days=1)
    rows = query_measurement_rows(
        MEASUREMENT,
        start=flux_time(start_dt),
        stop=flux_time(stop_dt),
        fields=(FIELD,),
        keep_columns=("_time", "_value", "serial_number"),
        sort_columns=("_time",),
    )

    bucket_values = defaultdict(dict)
    unmapped_telemetry_ids = set()
    unmapped_record_count = 0
    for row in rows:
        timestamp = row.get("_time")
        telemetry_id = row.get("serial_number")
        value = row.get("_value")
        if telemetry_id is None:
            unmapped_record_count += 1
            continue
        telemetry_id = str(telemetry_id)
        if telemetry_id not in panel_metadata:
            unmapped_telemetry_ids.add(telemetry_id)
            unmapped_record_count += 1
            continue
        if timestamp is None or value is None:
            continue
        local_time = timestamp.astimezone(local_tz)
        bucket_values[bucket_time(local_time, args.bucket_minutes)][telemetry_id] = float(value)

    baseline_start = parse_hhmm(args.baseline_start)
    baseline_stop = parse_hhmm(args.baseline_stop)
    analysis_start = parse_hhmm(args.analysis_start)
    analysis_stop = parse_hhmm(args.analysis_stop)
    serials = sorted({serial for values in bucket_values.values() for serial in values})

    ratios = defaultdict(list)
    fleet_rows = []
    for timestamp, values in sorted(bucket_values.items()):
        if len(values) < max(2, len(serials) - 1):
            continue
        values_by_face = defaultdict(list)
        for serial, watts in values.items():
            values_by_face[panel_metadata.get(serial, {}).get("roof_face", "unmapped")].append(watts)
        medians_by_face = {face: median(face_values) for face, face_values in values_by_face.items()}
        item = {
            "time": timestamp,
            "medians_by_face": medians_by_face,
            "values": values,
            "ratios": {},
        }
        for serial, watts in values.items():
            face = panel_metadata.get(serial, {}).get("roof_face", "unmapped")
            comparison_median = medians_by_face.get(face, 0.0)
            if comparison_median < args.minimum_fleet_median_watts:
                continue
            ratio = watts / comparison_median
            ratios[serial].append((timestamp, ratio, watts, comparison_median))
            item["ratios"][serial] = ratio
        if item["ratios"]:
            fleet_rows.append(item)

    baseline_ratio = {
        serial: median([ratio for timestamp, ratio, _, _ in items if in_window(timestamp, baseline_start, baseline_stop)])
        for serial, items in ratios.items()
    }

    results = []
    for serial in serials:
        baseline = baseline_ratio.get(serial, 0.0)
        if baseline <= 0:
            continue
        samples = []
        for timestamp, ratio, watts, fleet_median in ratios.get(serial, []):
            if in_window(timestamp, analysis_start, analysis_stop):
                samples.append(
                    {
                        "time": timestamp,
                        "normalized": ratio / baseline,
                        "watts": watts,
                        "expected_watts": baseline * fleet_median,
                    }
                )
        if not samples:
            continue
        normalized = [sample["normalized"] for sample in samples]
        onset = None
        for left, right in zip(samples, samples[1:]):
            if left["normalized"] < 0.85 and right["normalized"] < 0.85:
                onset = left["time"]
                break
        shortfall_wh = sum(
            max(sample["expected_watts"] - sample["watts"], 0.0) * args.bucket_minutes / 60.0
            for sample in samples
        )
        results.append(
            {
                "position_id": panel_metadata[serial]["position_id"],
                "roof_face": panel_metadata[serial]["roof_face"],
                "inverter_alias": panel_metadata[serial]["inverter_alias"],
                "channel_alias": panel_metadata[serial]["channel_alias"],
                "baseline_fleet_ratio": round(baseline, 3),
                "analysis_average_normalized": round(statistics.mean(normalized), 3),
                "analysis_minimum_normalized": round(min(normalized), 3),
                "first_sustained_below_85_percent": onset.strftime("%H:%M") if onset else None,
                "estimated_relative_shortfall_wh": round(shortfall_wh, 1),
            }
        )
    results.sort(key=lambda item: (item["analysis_average_normalized"], item["analysis_minimum_normalized"]))

    timeline = []
    for row in fleet_rows:
        timestamp = row["time"]
        if not in_window(timestamp, analysis_start, analysis_stop):
            continue
        normalized = {
            serial: ratio / baseline_ratio[serial]
            for serial, ratio in row["ratios"].items()
            if baseline_ratio.get(serial, 0.0) > 0
        }
        by_roof_face = {}
        for face in sorted({panel_metadata.get(serial, {}).get("roof_face") for serial in normalized}):
            face_values = {
                serial: value
                for serial, value in normalized.items()
                if panel_metadata.get(serial, {}).get("roof_face") == face
            }
            by_roof_face[face] = {
                "comparison_median_watts": round(row["medians_by_face"].get(face, 0.0), 1),
                "inverters_below_85_percent": sum(value < 0.85 for value in face_values.values()),
                "inverters_below_70_percent": sum(value < 0.70 for value in face_values.values()),
                "positions_below_85_percent": sorted(
                    panel_metadata[serial]["position_id"]
                    for serial, value in face_values.items()
                    if value < 0.85
                ),
                "positions_below_70_percent": sorted(
                    panel_metadata[serial]["position_id"]
                    for serial, value in face_values.items()
                    if value < 0.70
                ),
                "minimum_normalized": round(min(face_values.values()), 3) if face_values else None,
            }
        timeline.append({"time": timestamp.strftime("%H:%M"), "by_roof_face": by_roof_face})

    report = json.dumps(
        {
            "date": target_day.isoformat(),
            "timezone": "configured_local",
            "inverter_count": len(serials),
            "mapped_inverter_count": len(panel_metadata),
            "unmapped_records": {
                "record_count": unmapped_record_count,
                "distinct_inverter_count": len(unmapped_telemetry_ids),
                "identifiers": "omitted",
            },
            "raw_rows": len(rows),
            "bucket_minutes": args.bucket_minutes,
            "baseline_window": f"{args.baseline_start}-{args.baseline_stop}",
            "analysis_window": f"{args.analysis_start}-{args.analysis_stop}",
            "comparison_method": "Each inverter is normalized to the simultaneous median of its own roof face.",
            "inverters": results,
            "timeline": timeline,
        },
        indent=2,
    )
    if any(telemetry_id in report for telemetry_id in panel_metadata.keys() | unmapped_telemetry_ids):
        raise LayoutError("Report suppressed because it contains a private telemetry identifier.")
    print(report)


if __name__ == "__main__":
    try:
        main()
    except LayoutError as exc:
        print(f"analyze_inverter_shading: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print(
            "analyze_inverter_shading: analysis failed; exception details are withheld to protect local identifiers.",
            file=sys.stderr,
        )
        raise SystemExit(2)
