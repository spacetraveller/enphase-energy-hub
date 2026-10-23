"""Energy-conserving forecast calculations; no network or database side effects.

Irradiance is hourly. Quarter-hour shape comes from measured production, not
from a claim that the weather provider resolves individual clouds at 15 minutes.
"""
import math
from datetime import timedelta

from record.household_config import hot_water_estimate_window

VERSION = "interval-empirical-3"
FIELDS = {"energy_produced_wh": "Produced", "energy_consumed_wh": "Consumed",
          "exported_to_grid_wh": "Exported", "imported_from_grid_wh": "Imported"}
METRICS = tuple(FIELDS.values())


def mean(samples):
    weight = sum(w for _, w in samples)
    return sum(value * w for value, w in samples) / weight if weight else 0.0


def quantile(samples, fraction):
    samples = sorted((value, weight) for value, weight in samples if weight > 0)
    threshold = sum(weight for _, weight in samples) * fraction
    accumulated = 0.0
    for value, weight in samples:
        accumulated += weight
        if accumulated >= threshold:
            return value
    return samples[-1][0] if samples else 0.0


def demand_weight(target, day):
    # Recent household regime; avoid importing the pre-EV household into demand.
    return math.exp(-(target - day).days / 28.0) * (2.0 if target.weekday() == day.weekday() else 1.0)


def solar_weight(target, day):
    distance = abs(target.timetuple().tm_yday - day.timetuple().tm_yday)
    return math.exp(-(min(distance, 366 - distance) / 35.0) ** 2)


def fit_solar(days, radiation, target):
    """Fit hourly measured AC kWh / GHI kWh/m2, using only earlier days.

    The hourly response captures the combined north/west roof and shading. It is
    empirical, not a physical per-roof model requiring unconfirmed roof geometry.
    """
    train = sorted(day for day in days if target - timedelta(days=90) <= day < target
                   and day in radiation and len(radiation[day]) == 24)
    if len(train) < 28:
        return None
    coefficients = []
    for hour in range(24):
        numerator = denominator = 0.0
        for day in train:
            x = radiation[day][hour] / 1000.0
            y = sum(days[day]["energy_produced_wh"][hour * 4:hour * 4 + 4]) / 1000.0
            w = math.exp(-(target - day).days / 45.0)
            numerator += w * x * y
            denominator += w * x * x
        coefficients.append(numerator / denominator if denominator > 0.0001 else 0.0)
    return {"coefficients": coefficients, "days": len(train), "first_day": train[0].isoformat(),
            "last_day": train[-1].isoformat(), "method": "measured_hourly_ac_response_to_ghi"}


def production_profile(target, days, radiation=None, fit=None):
    candidates = [day for day in days if day < target]
    if not candidates:
        raise ValueError("No complete historical production days before target")
    # Historical quarter-hour shares preserve sunrise/sunset and afternoon bias.
    slots = [mean([(days[day]["energy_produced_wh"][slot] / 1000.0,
                    solar_weight(target, day)) for day in candidates]) for slot in range(96)]
    if radiation is not None and fit:
        for hour in range(24):
            total = sum(slots[hour * 4:hour * 4 + 4])
            expected = max(0.0, radiation[hour]) / 1000.0 * fit["coefficients"][hour]
            for slot in range(hour * 4, hour * 4 + 4):
                slots[slot] = expected * (slots[slot] / total if total > 0 else 0.25)
    return slots


def flow_rows(production, consumption, ev=None, hot_water=None):
    rows = []
    for slot, (produced, consumed) in enumerate(zip(production, consumption)):
        row = {"Hour": f"{slot // 4:02d}:{slot % 4 * 15:02d}", "Produced": produced,
               "Consumed": consumed, "Exported": max(0.0, produced - consumed),
               "Imported": max(0.0, consumed - produced)}
        if ev is not None:
            row["EV"] = ev[slot]
        if hot_water is not None:
            row["HotWaterEstimate"] = hot_water[slot]
        if ev is not None or hot_water is not None:
            row["HouseholdOther"] = max(
                0.0, consumed - (ev[slot] if ev is not None else 0.0)
                - (hot_water[slot] if hot_water is not None else 0.0)
            )
        rows.append(row)
    return rows


def aggregate(rows, slots_per_group):
    metrics = [key for key in rows[0] if key != "Hour"]
    return [{"Hour": rows[start]["Hour"],
             **{metric: sum(row[metric] for row in rows[start:start + slots_per_group])
                for metric in metrics}}
            for start in range(0, len(rows), slots_per_group)]


def totals(rows):
    return {metric: sum(row[metric] for row in rows) for metric in METRICS}


def choose_demand_window(target, days, regime_start=None):
    """Select recency using earlier rolling errors, never the target's load."""
    available = sorted(day for day in days if day < target and
                       (regime_start is None or day >= regime_start))
    validation = available[-14:]
    errors = {}
    for window in (14, 28, 56):
        values = []
        for test_day in validation:
            train = [day for day in available if test_day - timedelta(days=window) <= day < test_day]
            if len(train) < 10:
                continue
            expected = mean([(sum(days[day]["energy_consumed_wh"]) / 1000.0,
                              demand_weight(test_day, day)) for day in train])
            actual = sum(days[test_day]["energy_consumed_wh"]) / 1000.0
            values.append(abs(expected - actual))
        if len(values) == len(validation) and len(values) >= 10:
            errors[window] = sum(values) / len(values)
    return min(errors, key=errors.get) if errors else 28, errors


def _hot_water_estimate(load, start_slot, end_slot):
    """Estimate configured-window residual above nearby one-hour demand."""
    context = load[max(0, start_slot - 4):start_slot] + load[end_slot:min(96, end_slot + 4)]
    if not context:
        return None
    background = quantile([(value, 1.0) for value in context], 0.5)
    return [max(0.0, value - background) if start_slot <= slot < end_slot else 0.0
            for slot, value in enumerate(load)]


def demand_samples(target, days, ev_days, *, excluded_demand_days=(), ev_enabled=None,
                   regime_start=None, hot_water_window=None):
    ev_days = ev_days or {}
    if ev_enabled is None:
        # Keep the pure model convenient for callers that supply telemetry
        # directly; production callers pass the household setting explicitly.
        ev_enabled = bool(ev_days)
    excluded = sorted(day for day in days if day < target and day in excluded_demand_days)
    days = {day: values for day, values in days.items() if day not in excluded_demand_days}
    if not any(day < target for day in days):
        raise ValueError("No normal household days before target after calendar exclusions")
    window, validation = choose_demand_window(target, days, regime_start)
    cutoff = min(target, max(day for day in days if day < target) + timedelta(days=1))
    recent = sorted(day for day in days if cutoff - timedelta(days=window) <= day < target
                    and (regime_start is None or day >= regime_start))
    if len(recent) < 14:
        # A short chosen window may contain excluded days; use the longer window.
        window = 56
        recent = sorted(day for day in days if cutoff - timedelta(days=window) <= day < target
                        and (regime_start is None or day >= regime_start))
    if len(recent) < 14:
        raise ValueError("Need at least 14 complete household days in the last 56 days for demand modelling")
    paired = [day for day in recent if ev_enabled and day in ev_days and
              all(ev <= load + 30.0 for ev, load in
                  zip(ev_days[day], days[day]["energy_consumed_wh"]))]
    separated = len(paired) >= 14
    selected = paired if separated else recent
    samples = []
    for day in selected:
        load = [value / 1000.0 for value in days[day]["energy_consumed_wh"]]
        ev = [min(value / 1000.0, load[slot]) for slot, value in enumerate(ev_days[day])] if separated else [0.0] * 96
        non_ev = [max(0.0, load[slot] - ev[slot]) for slot in range(96)]
        # Descriptive estimate only. It never changes the observed load profile.
        hot = (_hot_water_estimate(non_ev, *hot_water_window)
               if hot_water_window is not None else None)
        samples.append({"day": day, "weight": demand_weight(target, day), "load": load,
                        "ev": ev, "non_ev": non_ev, "hot": hot})
    return samples, {"ev_separated": separated, "sample_days": len(samples),
                     "operating_regime": "normal_household",
                     "excluded_demand_dates": [day.isoformat() for day in excluded],
                     "window_days": window, "window_validation_mae_kwh": validation,
                     "paired_ev_days": len(paired), "recent_complete_days": len(recent),
                     "ev_enabled": bool(ev_enabled),
                     "ev_telemetry_available": bool(paired),
                     "first_day": selected[0].isoformat(), "last_day": selected[-1].isoformat(),
                     "hot_water_method": ("unconfirmed residual in configured window"
                                           if hot_water_window is not None else "unconfigured"),
                     "hot_water_window_slots": (list(hot_water_window)
                                                if hot_water_window is not None else None),
                     "ev_missing_is_zero": False}


def forecast_paths(production, samples, mode="expected"):
    if mode == "charging_day":
        samples = [sample for sample in samples if sum(sample["ev"]) >= 0.5]
        if not samples:
            return None
    paths = []
    for sample in samples:
        ev = sample["ev"] if mode != "no_ev" else [0.0] * 96
        load = [base + charge for base, charge in zip(sample["non_ev"], ev)]
        paths.append((flow_rows(production, load, ev, sample["hot"]), sample["weight"]))
    # Average flows AFTER netting each plausible day's load. Netting only the
    # average load erases charging/heater variation and understates grid imports.
    keys = [key for key in paths[0][0][0] if key != "Hour"]
    rows = [{"Hour": paths[0][0][slot]["Hour"],
             **{metric: mean([(path[slot][metric], weight) for path, weight in paths])
                for metric in keys}} for slot in range(96)]
    return {"slots": rows, "hourly": aggregate(rows, 4), "daily": totals(rows),
            "load_spread": {metric: {"low": quantile([(totals(path)[metric], weight) for path, weight in paths], .2),
                                     "high": quantile([(totals(path)[metric], weight) for path, weight in paths], .8)}
                            for metric in METRICS},
            "sample_days": len(paths)}


def predict(target, days, ev_days, radiation=None, radiation_history=None, *,
            excluded_demand_days=(), ev_enabled=None, regime_start=None,
            hot_water_window=None):
    if hot_water_window is None:
        hot_water_window = hot_water_estimate_window()
    fit = fit_solar(days, radiation_history or {}, target) if radiation is not None else None
    production = production_profile(target, days, radiation, fit)
    samples, load_info = demand_samples(target, days, ev_days, excluded_demand_days=excluded_demand_days,
                                        ev_enabled=ev_enabled, regime_start=regime_start,
                                        hot_water_window=hot_water_window)
    modes = (["expected", "no_ev", "charging_day"]
             if load_info["ev_enabled"] and load_info["ev_separated"] else ["expected"])
    scenarios = {mode: result for mode in modes if (result := forecast_paths(production, samples, mode))}
    if not load_info["ev_separated"]:
        for result in scenarios.values():
            for row in result["slots"] + result["hourly"]:
                for component in ("EV",):
                    row.pop(component, None)
    return {"scenarios": scenarios, "solar_fit": fit, "load_model": load_info,
            "solar_method": fit["method"] if fit else "seasonal_history_fallback",
            "resolution_note": "15-minute energy accounting; solar within-hour shape is historical",
            "flow_note": "Expected import/export average separate load scenarios; both may be positive in the mean."}


def rolling_residuals(days, ev_days, radiation_history, *, before, count=28,
                      excluded_demand_days=(), ev_enabled=None, regime_start=None,
                      hot_water_window=None):
    """Walk forward without target/future meter data. Archived weather is a
    reconstruction, NOT the forecast issued 1/2/3 days before each target.
    """
    results = []
    targets = sorted(day for day in days if day < before and day in radiation_history
                     and day not in excluded_demand_days)[-count:]
    for target in targets:
        try:
            window = hot_water_window
            if window is None:
                window = hot_water_estimate_window()
            result = predict(target, days, ev_days, radiation_history[target], radiation_history,
                             excluded_demand_days=excluded_demand_days, ev_enabled=ev_enabled,
                             regime_start=regime_start, hot_water_window=window)
        except ValueError:
            continue
        if not result["solar_fit"]:
            continue
        predicted = result["scenarios"]["expected"]["daily"]
        actual = {label: sum(days[target][field]) / 1000.0 for field, label in FIELDS.items()}
        results.append({"target_date": target.isoformat(), "actual": actual, "predicted": predicted,
                        "residual": {metric: actual[metric] - predicted[metric] for metric in METRICS}})
    return results


def residual_bounds(likely, metric, residuals, spread):
    if len(residuals) >= 14:
        errors = [(row["residual"][metric], 1.0) for row in residuals]
        return {"low": max(0.0, min(likely, likely + quantile(errors, .2))),
                "high": max(likely, likely + quantile(errors, .8))}
    return {"low": min(likely, spread["low"]), "high": max(likely, spread["high"])}
