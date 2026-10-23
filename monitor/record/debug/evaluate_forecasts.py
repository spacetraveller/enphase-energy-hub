"""Read-only retrospective model checks and genuine issued-forecast scoring."""
import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.history import forecast_model as model
from record.history.forecast_data import ROOT, historical_radiation, load_ev_days, published_forecasts, load_household_calendar
from record.history.predict_tomorrow import LOCAL_TZ, build_daily_and_hourly, fetch_history, forecast_daily


def scores(pairs):
    errors = [predicted - actual for predicted, actual, _, _ in pairs]
    n = len(errors)
    return {"days": n, "mae_kwh": round(sum(abs(value) for value in errors) / n, 3),
            "bias_kwh": round(sum(errors) / n, 3),
            "rmse_kwh": round(math.sqrt(sum(value * value for value in errors) / n), 3),
            "interval_coverage": round(sum(low <= actual <= high for _, actual, low, high in pairs) / n, 3)} if n else {"days": 0}


def issued_scores(directory, days, excluded_demand_days=()):
    latest = published_forecasts(directory, LOCAL_TZ)
    grouped = defaultdict(list)
    for (version, lead, target), (_, forecast) in latest.items():
        if lead < 1 or target not in days or target in excluded_demand_days:
            continue
        for field, metric in model.FIELDS.items():
            value = forecast["weather_adjusted_daily_kwh"][metric]
            grouped[(version, lead, metric)].append((value["likely"], sum(days[target][field]) / 1000.0,
                                                   value["low"], value["high"]))
    return [{"version": version, "lead_days": lead, "metric": metric, **scores(pairs)}
            for (version, lead, metric), pairs in sorted(grouped.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrospective", action="store_true")
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--before", type=date.fromisoformat, default=datetime.now(LOCAL_TZ).date())
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "forecast_archive")
    args = parser.parse_args()
    daily, _ = build_daily_and_hourly(fetch_history())
    excluded, calendar = load_household_calendar()
    normal_daily = {day: values for day, values in daily.items() if day not in excluded}
    result = {"history_quality": daily.quality, "issued_forecasts": issued_scores(args.archive_dir, daily.slots, excluded),
              "household_calendar": calendar, "evaluation_regime": "normal_household",
              "issuance_selection": "Latest published issue per local target date, lead day and model version"}
    if args.retrospective:
        radiation = historical_radiation(LOCAL_TZ)
        ev = load_ev_days(LOCAL_TZ, args.before)
        residuals = model.rolling_residuals(daily.slots, ev, radiation, before=args.before, count=args.days + 28,
                                          excluded_demand_days=excluded)
        grouped = defaultdict(list)
        for index, row in enumerate(residuals):
            if index < len(residuals) - args.days:
                continue
            target = date.fromisoformat(row["target_date"])
            old = forecast_daily(target, normal_daily)
            for metric in model.METRICS:
                actual = row["actual"][metric]
                likely = row["predicted"][metric]
                bounds = model.residual_bounds(likely, metric, residuals[max(0, index - 28):index],
                                               {"low": likely, "high": likely})
                grouped[("empirical_intervals", metric)].append((likely, actual, bounds["low"], bounds["high"]))
                previous = old[metric]
                grouped[("old_historical_baseline", metric)].append((previous.likely, actual, previous.low, previous.high))
        result["retrospective"] = {
            "note": "Rolling meter-data holdouts using reconstructed near-time weather, NOT issued 1-3 day forecasts. Bounds use only earlier residuals.",
            "first_target": residuals[-args.days:][0]["target_date"] if residuals else None,
            "last_target": residuals[-1]["target_date"] if residuals else None,
            "metrics": [{"model": name, "metric": metric, **scores(pairs)} for (name, metric), pairs in sorted(grouped.items())]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
