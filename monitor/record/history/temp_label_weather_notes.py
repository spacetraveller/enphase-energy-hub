import argparse
import csv
import math
import shutil
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


DEFAULT_CSV = (
    Path(__file__).resolve().parent
    / "exports"
    / "ai_worker_energy_history.csv"
)
SLOTS_PER_DAY = 96


def correlation(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_denom = math.sqrt(sum((a - left_mean) ** 2 for a in left))
    right_denom = math.sqrt(sum((b - right_mean) ** 2 for b in right))
    denom = left_denom * right_denom
    if denom <= 0:
        return 0.0
    return numerator / denom


def percentile_rank(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return sum(1 for item in values if item <= value) / len(values)


def day_vector(items: list[tuple[datetime, float]]) -> list[float] | None:
    vector = [0.0] * SLOTS_PER_DAY
    seen = set()
    for timestamp, value in items:
        index = timestamp.hour * 4 + timestamp.minute // 15
        if 0 <= index < SLOTS_PER_DAY:
            vector[index] = value
            seen.add(index)
    if len(seen) < 92:
        return None
    return vector


def day_metrics(items: list[tuple[datetime, float]]) -> dict[str, float | int | str] | None:
    vector = day_vector(items)
    if not vector:
        return None

    total = sum(vector)
    peak_15m = max(vector)
    daylight_values = [value for value in vector if value > 0.005]
    if len(daylight_values) < 8 or peak_15m <= 0:
        return None

    drops = [
        previous - current
        for previous, current in zip(daylight_values, daylight_values[1:])
        if current < previous
    ]
    big_drops = sum(1 for drop in drops if drop >= max(0.08, peak_15m * 0.08))
    roughness = (
        sum(abs(current - previous) for previous, current in zip(daylight_values, daylight_values[1:]))
        / max(total, 0.001)
    )
    high_values = [value for value in daylight_values if value >= peak_15m * 0.7]
    high_cv = 1.0
    if len(high_values) > 2 and statistics.mean(high_values):
        high_cv = statistics.pstdev(high_values) / statistics.mean(high_values)

    return {
        "total": total,
        "peak_kw": peak_15m * 4,
        "midday_total": sum(vector[10 * 4 : 14 * 4]),
        "midday_peak_kw": max(vector[10 * 4 : 14 * 4]) * 4,
        "big_drops": big_drops,
        "roughness": roughness,
        "high_cv": high_cv,
    }


def score_template_candidates(days: dict[str, dict[str, float | int | str]]) -> None:
    by_month = defaultdict(list)
    for date_text, metrics in days.items():
        month = int(date_text[5:7])
        metrics["date"] = date_text
        by_month[month].append(metrics)

    for month_days in by_month.values():
        totals = [float(day["total"]) for day in month_days]
        peaks = [float(day["peak_kw"]) for day in month_days]
        roughnesses = [float(day["roughness"]) for day in month_days]
        cvs = [float(day["high_cv"]) for day in month_days]
        drop_counts = [float(day["big_drops"]) for day in month_days]

        for day in month_days:
            total_p = percentile_rank(totals, float(day["total"]))
            peak_p = percentile_rank(peaks, float(day["peak_kw"]))
            roughness_p = 1 - percentile_rank(roughnesses, float(day["roughness"]))
            cv_p = 1 - percentile_rank(cvs, float(day["high_cv"]))
            drop_p = 1 - percentile_rank(drop_counts, float(day["big_drops"]))
            day["template_score"] = 100 * (
                0.50 * total_p
                + 0.20 * peak_p
                + 0.15 * roughness_p
                + 0.10 * cv_p
                + 0.05 * drop_p
            )


def build_month_templates(
    rows_by_day: dict[str, list[tuple[datetime, float]]],
    days: dict[str, dict[str, float | int | str]],
) -> dict[int, dict[str, float | list[float]]]:
    by_month = defaultdict(list)
    for date_text, metrics in days.items():
        by_month[int(date_text[5:7])].append(metrics)

    templates = {}
    for month, month_days in by_month.items():
        selected = sorted(month_days, key=lambda day: float(day["template_score"]), reverse=True)[:5]
        vectors = []
        for day in selected:
            vector = day_vector(rows_by_day[str(day["date"])])
            if vector:
                vectors.append(vector)
        if not vectors:
            continue
        template_vector = [
            sum(vector[index] for vector in vectors) / len(vectors)
            for index in range(SLOTS_PER_DAY)
        ]
        templates[month] = {
            "vector": template_vector,
            "total": sum(template_vector),
            "peak_kw": max(template_vector) * 4,
        }
    return templates


def classify_day(
    metrics: dict[str, float | int | str],
    template: dict[str, float | list[float]],
    vector: list[float],
) -> str:
    template_vector = template["vector"]
    assert isinstance(template_vector, list)
    energy_ratio = float(metrics["total"]) / max(float(template["total"]), 0.001)
    peak_ratio = float(metrics["peak_kw"]) / max(float(template["peak_kw"]), 0.001)
    shape_corr = correlation(vector, template_vector)
    midday_peak_kw = float(metrics["midday_peak_kw"])
    roughness = float(metrics["roughness"])
    big_drops = int(metrics["big_drops"])

    # Sunny allows ordinary tiny cloud wisps; "perfect/no detectable cloud" is stricter
    # and lives in analyze_sunny_templates.py.
    if (
        energy_ratio >= 0.86
        and peak_ratio >= 0.88
        and shape_corr >= 0.970
        and big_drops <= 6
        and roughness <= 0.15
    ):
        return "sunny"

    if energy_ratio <= 0.18 and peak_ratio <= 0.30 and midday_peak_kw <= 1.35:
        return "heavy rain"
    if energy_ratio <= 0.32 and peak_ratio <= 0.45 and midday_peak_kw <= 2.00:
        return "rain"
    if energy_ratio <= 0.62 or peak_ratio <= 0.68 or midday_peak_kw <= 3.20:
        return "cloudy"
    return "partly cloudy"


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header")
        return rows, reader.fieldnames


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()

    rows, fieldnames = load_rows(args.csv)
    if "notes" not in fieldnames:
        fieldnames.append("notes")
        for row in rows:
            row["notes"] = ""

    rows_by_day = defaultdict(list)
    for row in rows:
        timestamp = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
        rows_by_day[timestamp.date().isoformat()].append(
            (timestamp, float(row["solar_production_kwh"]))
        )

    days = {}
    for date_text, items in rows_by_day.items():
        metrics = day_metrics(items)
        if metrics:
            days[date_text] = metrics

    score_template_candidates(days)
    templates = build_month_templates(rows_by_day, days)
    labels = {}
    label_metrics = {}
    for date_text, metrics in days.items():
        month = int(date_text[5:7])
        vector = day_vector(rows_by_day[date_text])
        if not vector or month not in templates:
            continue
        label = classify_day(metrics, templates[month], vector)
        labels[date_text] = label
        label_metrics[date_text] = {
            **metrics,
            "energy_ratio": float(metrics["total"]) / max(float(templates[month]["total"]), 0.001),
            "peak_ratio": float(metrics["peak_kw"]) / max(float(templates[month]["peak_kw"]), 0.001),
        }

    counts = Counter(labels.values())
    print(f"days_classified={len(labels)} rows={len(rows)}")
    for label in ("sunny", "partly cloudy", "cloudy", "rain", "heavy rain"):
        print(f"{label}={counts[label]}")

    print("examples")
    for label in ("sunny", "partly cloudy", "cloudy", "rain", "heavy rain"):
        examples = [date_text for date_text, value in labels.items() if value == label][:5]
        print(f"{label}: {', '.join(examples)}")

    if not args.write:
        print("dry_run=true")
        return

    if args.backup:
        backup_path = args.csv.with_suffix(args.csv.suffix + ".bak")
        shutil.copy2(args.csv, backup_path)
        print(f"backup={backup_path}")

    for row in rows:
        day = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").date().isoformat()
        row["notes"] = labels.get(day, row.get("notes", ""))

    with args.csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"updated={args.csv}")


if __name__ == "__main__":
    main()
