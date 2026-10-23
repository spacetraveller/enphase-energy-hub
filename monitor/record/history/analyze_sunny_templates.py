import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.query_helpers import env_timezone

LOCAL_TZ = ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
MEASUREMENT = "enphase_history_15m"
PRODUCTION_FIELD = "energy_produced_wh"
SLOTS_PER_DAY = 96


def season_for(month: int) -> str:
    if month in (12, 1, 2):
        return "Summer"
    if month in (3, 4, 5):
        return "Autumn"
    if month in (6, 7, 8):
        return "Winter"
    return "Spring"


def percentile_rank(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return sum(1 for item in values if item <= value) / len(values)


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


def day_metrics(items: list[tuple[datetime, float]]) -> dict[str, float | int | str] | None:
    items = sorted(items)
    values = [value for _, value in items]
    total = sum(values)
    peak = max(values) if values else 0.0
    daylight = [(timestamp, value) for timestamp, value in items if value > 0.005]

    if len(daylight) < 8 or peak <= 0:
        return None

    first_index = items.index(daylight[0])
    last_index = items.index(daylight[-1])
    span = max(last_index - first_index, 1)
    xs = []
    ys = []

    for index in range(first_index, last_index + 1):
        value = items[index][1]
        x = math.sin(math.pi * (index - first_index) / span)
        if x > 0.02 or value > 0.005:
            xs.append(x)
            ys.append(value)

    denom = sum(x * x for x in xs) or 1.0
    amplitude = sum(x * y for x, y in zip(xs, ys)) / denom
    predicted = [amplitude * x for x in xs]
    sse = sum((y - prediction) ** 2 for y, prediction in zip(ys, predicted))
    mean_y = sum(ys) / len(ys)
    sst = sum((y - mean_y) ** 2 for y in ys) or 1.0
    r2 = 1 - sse / sst

    daylight_values = [value for _, value in daylight]
    drops = [
        previous - current
        for previous, current in zip(daylight_values, daylight_values[1:])
        if current < previous
    ]
    big_drops = sum(1 for drop in drops if drop >= max(0.08, peak * 0.08))
    roughness = (
        sum(abs(current - previous) for previous, current in zip(daylight_values, daylight_values[1:]))
        / max(total, 0.001)
    )
    high_values = [value for value in daylight_values if value >= peak * 0.7]
    high_cv = 1.0
    if len(high_values) > 2 and statistics.mean(high_values):
        high_cv = statistics.pstdev(high_values) / statistics.mean(high_values)

    return {
        "rows": len(items),
        "total": total,
        "peak_15m": peak,
        "peak_kw": peak * 4,
        "daylight_hours": len(daylight_values) / 4,
        "first": daylight[0][0].strftime("%H:%M"),
        "last": daylight[-1][0].strftime("%H:%M"),
        "r2": r2,
        "big_drops": big_drops,
        "roughness": roughness,
        "high_cv": high_cv,
        "completeness": min(len(items) / 96, 1.0),
    }


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


def build_month_templates(
    rows_by_day: defaultdict, daily: list[dict[str, float | int | str]]
) -> dict[int, dict[str, float | list[float]]]:
    templates = {}
    by_month = defaultdict(list)
    for day in daily:
        month = datetime.strptime(str(day["date"]), "%Y-%m-%d").month
        by_month[month].append(day)

    for month, month_days in by_month.items():
        ordered = sorted(month_days, key=lambda day: float(day["score"]), reverse=True)
        selected = ordered[: min(5, len(ordered))]
        vectors = []
        for day in selected:
            target_day = datetime.strptime(str(day["date"]), "%Y-%m-%d").date()
            vector = day_vector(rows_by_day[target_day])
            if vector:
                vectors.append(vector)
        if not vectors:
            continue
        template = [
            sum(vector[index] for vector in vectors) / len(vectors)
            for index in range(SLOTS_PER_DAY)
        ]
        templates[month] = {
            "vector": template,
            "total": sum(template),
            "peak_kw": max(template) * 4,
        }
    return templates


def classify_clear_days(
    rows_by_day: defaultdict,
    daily: list[dict[str, float | int | str]],
    templates: dict[int, dict[str, float | list[float]]],
) -> list[dict[str, float | int | str]]:
    if not daily:
        return []

    latest = max(datetime.strptime(str(day["date"]), "%Y-%m-%d").date() for day in daily)
    start = latest - timedelta(days=364)
    clear_days = []

    for day in daily:
        target_day = datetime.strptime(str(day["date"]), "%Y-%m-%d").date()
        if target_day < start or target_day > latest:
            continue
        template = templates.get(target_day.month)
        vector = day_vector(rows_by_day[target_day])
        if not template or not vector:
            continue
        template_vector = template["vector"]
        assert isinstance(template_vector, list)
        energy_ratio = float(day["total"]) / max(float(template["total"]), 0.001)
        peak_ratio = float(day["peak_kw"]) / max(float(template["peak_kw"]), 0.001)
        shape_corr = correlation(vector, template_vector)
        day["template_energy_ratio"] = energy_ratio
        day["template_peak_ratio"] = peak_ratio
        day["template_correlation"] = shape_corr

        # "No detectable cloud" is intentionally strict: close to the best month-specific
        # clear-day energy, close to clear-day peak, and very similar 15-minute curve shape.
        if (
            energy_ratio >= 0.92
            and peak_ratio >= 0.93
            and shape_corr >= 0.985
            and int(day["big_drops"]) <= 4
            and float(day["roughness"]) <= 0.11
        ):
            clear_days.append(day)

    clear_days.sort(key=lambda day: str(day["date"]))
    return clear_days


def classify_cloudy_days(
    rows_by_day: defaultdict,
    daily: list[dict[str, float | int | str]],
    templates: dict[int, dict[str, float | list[float]]],
) -> list[dict[str, float | int | str]]:
    if not daily:
        return []

    latest = max(datetime.strptime(str(day["date"]), "%Y-%m-%d").date() for day in daily)
    start = latest - timedelta(days=364)
    cloudy_days = []

    for day in daily:
        target_day = datetime.strptime(str(day["date"]), "%Y-%m-%d").date()
        if target_day < start or target_day > latest:
            continue
        template = templates.get(target_day.month)
        vector = day_vector(rows_by_day[target_day])
        if not template or not vector:
            continue
        template_vector = template["vector"]
        assert isinstance(template_vector, list)
        energy_ratio = float(day["total"]) / max(float(template["total"]), 0.001)
        peak_ratio = float(day["peak_kw"]) / max(float(template["peak_kw"]), 0.001)
        shape_corr = correlation(vector, template_vector)
        midday_slots = vector[10 * 4 : 14 * 4]
        midday_total = sum(midday_slots)
        midday_peak_kw = max(midday_slots or [0.0]) * 4
        day["template_energy_ratio"] = energy_ratio
        day["template_peak_ratio"] = peak_ratio
        day["template_correlation"] = shape_corr
        day["midday_total"] = midday_total
        day["midday_peak_kw"] = midday_peak_kw
        day["cloud_score"] = (
            (1 - min(energy_ratio, 1.0)) * 55
            + (1 - min(peak_ratio, 1.0)) * 30
            + (1 - min(midday_peak_kw / 2.0, 1.0)) * 15
        )

        # Full-day heavy cloud/rain template: production is suppressed all day,
        # including the strongest 10:00-14:00 solar window.
        if energy_ratio <= 0.32 and peak_ratio <= 0.45 and midday_peak_kw <= 2.0:
            cloudy_days.append(day)

    cloudy_days.sort(key=lambda day: float(day["cloud_score"]), reverse=True)
    return cloudy_days


def load_days() -> defaultdict:
    bucket = os.environ["INFLUXDB_BUCKET"]
    org = os.environ["INFLUXDB_ORG"]
    start = os.getenv("SUNNY_TEMPLATE_START", "2020-01-01T00:00:00Z")
    query = f'''
from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{PRODUCTION_FIELD}")
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
'''
    client = InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=org,
    )
    rows_by_day = defaultdict(list)
    for table in client.query_api().query(query, org=org):
        for record in table.records:
            timestamp = record.get_time().astimezone(LOCAL_TZ)
            production_kwh = float(record.get_value()) / 1000.0
            rows_by_day[timestamp.date()].append((timestamp, production_kwh))
    client.close()
    return rows_by_day


def main() -> None:
    rows_by_day = load_days()
    daily = []

    for day, items in rows_by_day.items():
        metrics = day_metrics(items)
        if metrics and metrics["rows"] >= 92:
            metrics["date"] = day.isoformat()
            metrics["season"] = season_for(day.month)
            daily.append(metrics)

    by_season = defaultdict(list)
    for metrics in daily:
        by_season[metrics["season"]].append(metrics)

    for season, season_days in by_season.items():
        totals = [day["total"] for day in season_days]
        peaks = [day["peak_kw"] for day in season_days]
        r2s = [day["r2"] for day in season_days]
        roughnesses = [day["roughness"] for day in season_days]
        cvs = [day["high_cv"] for day in season_days]
        drop_counts = [day["big_drops"] for day in season_days]

        for day in season_days:
            total_p = percentile_rank(totals, day["total"])
            peak_p = percentile_rank(peaks, day["peak_kw"])
            r2_p = percentile_rank(r2s, day["r2"])
            roughness_p = 1 - percentile_rank(roughnesses, day["roughness"])
            cv_p = 1 - percentile_rank(cvs, day["high_cv"])
            drop_p = 1 - percentile_rank(drop_counts, day["big_drops"])
            day["score"] = (
                100
                * (
                    0.40 * total_p
                    + 0.20 * peak_p
                    + 0.20 * r2_p
                    + 0.10 * roughness_p
                    + 0.05 * cv_p
                    + 0.05 * drop_p
                )
                * day["completeness"]
            )

    templates = build_month_templates(rows_by_day, daily)
    clear_days = classify_clear_days(rows_by_day, daily, templates)
    cloudy_days = classify_cloudy_days(rows_by_day, daily, templates)

    print(
        "DATASET days={days} first={first} last={last}".format(
            days=len(daily),
            first=min(rows_by_day),
            last=max(rows_by_day),
        )
    )

    if daily:
        latest = max(datetime.strptime(str(day["date"]), "%Y-%m-%d").date() for day in daily)
        start = latest - timedelta(days=364)
        print(
            "CLEAR_DAY_COUNT start={start} stop={stop} days={days} criteria=energy>=92%_monthly_template peak>=93%_monthly_template correlation>=0.985 drops<=4 roughness<=0.11".format(
                start=start,
                stop=latest,
                days=len(clear_days),
            )
        )
        clear_by_season = defaultdict(int)
        clear_by_month = defaultdict(int)
        for day in clear_days:
            target_day = datetime.strptime(str(day["date"]), "%Y-%m-%d").date()
            clear_by_season[str(day["season"])] += 1
            clear_by_month[target_day.strftime("%Y-%m")] += 1
        print(
            "CLEAR_BY_SEASON "
            + " ".join(
                f"{season}={clear_by_season[season]}"
                for season in ("Summer", "Autumn", "Winter", "Spring")
            )
        )
        print(
            "CLEAR_BY_MONTH "
            + " ".join(
                f"{month}={clear_by_month[month]}"
                for month in sorted(clear_by_month)
            )
        )
        print("CLEAR_DAYS")
        for day in clear_days:
            print(
                "{date} season={season} total={total:.2f}kWh peak={peak_kw:.2f}kW corr={template_correlation:.3f} energy_ratio={template_energy_ratio:.3f} drops={big_drops} rough={roughness:.3f}".format(
                    **day
                )
            )
        print(
            "CLOUDY_DAY_COUNT start={start} stop={stop} days={days} criteria=energy<=32%_monthly_template peak<=45%_monthly_template midday_peak<=2.0kW".format(
                start=start,
                stop=latest,
                days=len(cloudy_days),
            )
        )
        cloudy_by_season = defaultdict(int)
        cloudy_by_month = defaultdict(int)
        for day in cloudy_days:
            target_day = datetime.strptime(str(day["date"]), "%Y-%m-%d").date()
            cloudy_by_season[str(day["season"])] += 1
            cloudy_by_month[target_day.strftime("%Y-%m")] += 1
        print(
            "CLOUDY_BY_SEASON "
            + " ".join(
                f"{season}={cloudy_by_season[season]}"
                for season in ("Summer", "Autumn", "Winter", "Spring")
            )
        )
        print(
            "CLOUDY_BY_MONTH "
            + " ".join(
                f"{month}={cloudy_by_month[month]}"
                for month in sorted(cloudy_by_month)
            )
        )
        print("CLOUDY_DAYS")
        for day in cloudy_days[:30]:
            print(
                "{date} season={season} cloud_score={cloud_score:.1f} total={total:.2f}kWh peak={peak_kw:.2f}kW midday={midday_total:.2f}kWh midday_peak={midday_peak_kw:.2f}kW energy_ratio={template_energy_ratio:.3f} peak_ratio={template_peak_ratio:.3f} corr={template_correlation:.3f}".format(
                    **day
                )
            )

    for season in ("Summer", "Autumn", "Winter", "Spring"):
        candidates = sorted(by_season[season], key=lambda day: day["score"], reverse=True)
        print(f"\n{season.upper()} candidate_days={len(candidates)}")
        for day in candidates[:8]:
            print(
                "{date} score={score:.1f} total={total:.2f}kWh peak={peak_kw:.2f}kW "
                "daylight={daylight_hours:.2f}h {first}-{last} r2={r2:.3f} "
                "drops={big_drops} rough={roughness:.3f} high_cv={high_cv:.3f}".format(**day)
            )

    for season in ("Summer", "Autumn", "Winter", "Spring"):
        top = sorted(by_season[season], key=lambda day: day["score"], reverse=True)[0]
        target_day = datetime.strptime(top["date"], "%Y-%m-%d").date()
        items = sorted(rows_by_day[target_day])
        peak_timestamp, _ = max(items, key=lambda item: item[1])
        hourly = defaultdict(float)
        for timestamp, value in items:
            hourly[timestamp.hour] += value
        profile = " ".join(
            f"{hour:02d}:{hourly[hour]:.2f}"
            for hour in range(5, 19)
            if hourly[hour] > 0.005
        )
        print(
            "\nPROFILE {season} {date} total={total:.2f} peak={peak_kw:.2f}kW at {peak_time}".format(
                season=season,
                date=top["date"],
                total=top["total"],
                peak_kw=top["peak_kw"],
                peak_time=peak_timestamp.strftime("%H:%M"),
            )
        )
        print(profile)


if __name__ == "__main__":
    main()
