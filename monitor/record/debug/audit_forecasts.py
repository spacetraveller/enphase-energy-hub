"""Read-only forecast consistency checks and rolling historical baseline evaluation."""

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from record.history.predict_tomorrow import FIELDS, LOCAL_TZ, day_weight, forecast_daily
from record.query_helpers import flux_time, query_measurement_rows


def boundary(day):
    return flux_time(datetime.combine(day, time.min, tzinfo=LOCAL_TZ))


def scores(pairs):
    if not pairs:
        return {"days": 0}
    errors = [predicted - actual for actual, predicted in pairs]
    return {
        "days": len(pairs),
        "actual_mean_kwh": round(statistics.mean(a for a, _ in pairs), 3),
        "predicted_mean_kwh": round(statistics.mean(p for _, p in pairs), 3),
        "mae_kwh": round(statistics.mean(abs(e) for e in errors), 3),
        "bias_kwh": round(statistics.mean(errors), 3),
        "rmse_kwh": round(math.sqrt(statistics.mean(e * e for e in errors)), 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat,
                        help="First evaluation date (inclusive).")
    parser.add_argument("--end-date", required=True, type=date.fromisoformat,
                        help="Last evaluation date (inclusive).")
    parser.add_argument("--forecast-start", required=True, type=date.fromisoformat)
    parser.add_argument("--forecast-days", type=int, default=3)
    parser.add_argument("--history-start", type=date.fromisoformat, default=date(2024, 10, 1))
    parser.add_argument("--regime-start", type=date.fromisoformat, default=date(2025, 12, 11))
    args = parser.parse_args()
    if args.start_date > args.end_date or args.forecast_days < 1:
        parser.error("Require start-date <= end-date and forecast-days >= 1")
    now = datetime.now(LOCAL_TZ)
    history_stop = min(now.date(), max(args.end_date + timedelta(days=1), args.forecast_start))
    unique = {}
    duplicates = 0
    for row in query_measurement_rows(
        "enphase_history_15m", start=boundary(args.history_start), stop=boundary(history_stop),
        fields=FIELDS, keep_columns=["_time", "_field", "_value", "source"],
    ):
        key = (row["_time"].astimezone(LOCAL_TZ), row["_field"])
        priority = int(row.get("source") == "enlighten_export")
        if key in unique:
            duplicates += 1
        if key not in unique or priority >= unique[key][0]:
            unique[key] = (priority, float(row["_value"]))
    daily = defaultdict(lambda: defaultdict(float))
    counts = defaultdict(lambda: defaultdict(set))
    for (timestamp, field), (_, value) in unique.items():
        daily[timestamp.date()][field] += value
        counts[timestamp.date()][field].add(timestamp)
    complete = {d: v for d, v in daily.items()
                if all(len(counts[d][f]) == 96 for f in FIELDS)}
    incomplete = {str(d): {f: len(counts[d][f]) for f in FIELDS}
                  for d in sorted(daily) if d not in complete}

    # Each target is forecast using only days strictly before it. This tests the
    # historical component, not weather adjustment or original data availability.
    pairs = defaultdict(list)
    coverage = defaultdict(list)
    for target in sorted(complete):
        if not args.start_date <= target <= args.end_date:
            continue
        training = {d: v for d, v in daily.items() if d < target}
        if not training:
            continue
        forecasts = forecast_daily(target, training)
        for field, label in FIELDS.items():
            actual = complete[target][field] / 1000
            prediction = forecasts[label]
            pairs[(label, "current_historical")].append((actual, prediction.likely))
            coverage[label].append(prediction.low <= actual <= prediction.high)
            for name, days in (("trailing_28_day_mean", 28), ("trailing_56_day_same_weekday", 56)):
                recent = [v[field] / 1000 for d, v in complete.items()
                          if target - timedelta(days=days) <= d < target
                          and (name != "trailing_56_day_same_weekday" or d.weekday() == target.weekday())]
                if recent:
                    pairs[(label, name)].append((actual, statistics.mean(recent)))
    backtest = {label: {name: scores(items) for (metric, name), items in pairs.items() if metric == label}
                for label in FIELDS.values()}
    for label, values in coverage.items():
        backtest[label]["current_historical"]["p20_p80_coverage_pct"] = round(100 * statistics.mean(values), 1)

    start = boundary(args.forecast_start)
    stop = boundary(args.forecast_start + timedelta(days=args.forecast_days))
    current = defaultdict(lambda: {"daily": defaultdict(dict), "hourly_sum": defaultdict(float), "hourly_count": defaultdict(int)})
    for row in query_measurement_rows("enphase_prediction_daily", start=start, stop=stop,
            filters=['r.model == "weather_adjusted"'], fields=["likely_kwh", "low_kwh", "high_kwh"]):
        current[row["target_date"]]["daily"][row["metric"]][row["_field"]] = row["_value"]
    for row in query_measurement_rows("enphase_prediction_daily", start=start, stop=stop,
            filters=['r.model == "weather_adjusted"'], fields=["fetched_at_unix"]):
        current[row["target_date"]]["daily"][row["metric"]]["fetched_at"] = datetime.fromtimestamp(row["_value"], LOCAL_TZ).isoformat()
    for row in query_measurement_rows("enphase_prediction_hourly", start=start, stop=stop,
            filters=['r.model == "weather_adjusted"'], fields=["kwh"]):
        item = current[row["target_date"]]
        item["hourly_sum"][row["metric"]] += float(row["_value"])
        item["hourly_count"][row["metric"]] += 1
    for item in current.values():
        item["hourly_sum"] = {k: round(v, 3) for k, v in item["hourly_sum"].items()}
        item["daily_minus_hourly_kwh"] = {k: round(v["likely_kwh"] - item["hourly_sum"].get(k, 0), 3)
                                           for k, v in item["daily"].items()}
        item["daily_balance_residual_kwh"] = round(sum(item["daily"].get(k, {}).get("likely_kwh", 0) * sign
            for k, sign in (("Produced", 1), ("Imported", 1), ("Consumed", -1), ("Exported", -1))), 3)
    weights = {d: day_weight(args.forecast_start, d, max(daily)) for d in daily} if daily else {}
    print(json.dumps({
        "evaluation_range": [str(args.start_date), str(args.end_date)],
        "history": {"days_present": len(daily), "complete_days": len(complete),
                    "first": str(min(daily)) if daily else None, "last": str(max(daily)) if daily else None,
                    "duplicate_field_timestamps": duplicates, "incomplete_days": incomplete},
        "pre_regime_weight_pct": round(100 * sum(w for d, w in weights.items() if d < args.regime_start) / sum(weights.values()), 1) if weights else None,
        "backtest_note": "Historical component only; target and later days excluded. Uses current imported history, not original issuance snapshots. Negative bias means underprediction.",
        "backtest": backtest,
        "stored_forecasts": current,
    }, indent=2))


if __name__ == "__main__":
    main()
