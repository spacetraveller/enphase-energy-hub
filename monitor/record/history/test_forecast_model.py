import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from record.history import forecast_model as model
from record.history.forecast_data import archive_forecasts, load_weather, radiation_days, mark_published, published_forecasts, load_household_calendar
from record.debug.assess_ev_charging_window import window_hours
from record.history.predict_tomorrow import Forecast, write_forecasts
from record.query_helpers import complete_interval_days

TZ = ZoneInfo("UTC")


class ForecastTests(unittest.TestCase):
    def fixture(self):
        target = date(2026, 10, 6)
        days, ev, radiation = {}, {}, {}
        for age in range(1, 101):
            day = target - timedelta(days=age)
            solar = [250.0 if 24 <= slot < 72 else 0.0 for slot in range(96)]
            charge = [500.0 if age % 2 and 44 <= slot < 52 else 0.0 for slot in range(96)]
            load = [100.0 + value for value in charge]
            days[day] = {"energy_produced_wh": solar, "energy_consumed_wh": load,
                         "exported_to_grid_wh": [max(0, p - c) for p, c in zip(solar, load)],
                         "imported_from_grid_wh": [max(0, c - p) for p, c in zip(solar, load)]}
            ev[day] = charge
            radiation[day] = [250.0 if 6 <= hour < 18 else 0.0 for hour in range(24)]
        return target, days, ev, radiation

    def test_flow_balance_all_resolutions_and_scenarios(self):
        target, days, ev, radiation = self.fixture()
        result = model.predict(target, days, ev, next(iter(radiation.values())), radiation)
        for scenario in result["scenarios"].values():
            for row in scenario["slots"] + scenario["hourly"] + [scenario["daily"]]:
                self.assertAlmostEqual(row["Produced"] + row["Imported"], row["Consumed"] + row["Exported"])
                self.assertTrue(all(row[key] >= 0 for key in model.METRICS))
            for metric in model.METRICS:
                self.assertAlmostEqual(sum(row[metric] for row in scenario["hourly"]), scenario["daily"][metric])
        self.assertLess(result["scenarios"]["no_ev"]["daily"]["Consumed"], result["scenarios"]["expected"]["daily"]["Consumed"])
        self.assertGreater(result["scenarios"]["charging_day"]["daily"]["Consumed"], result["scenarios"]["expected"]["daily"]["Consumed"])

    def test_missing_ev_is_not_zero(self):
        target, days, _, radiation = self.fixture()
        result = model.predict(target, days, {}, next(iter(radiation.values())), radiation)
        self.assertFalse(result["load_model"]["ev_separated"])
        self.assertEqual(set(result["scenarios"]), {"expected"})
        self.assertNotIn("EV", result["scenarios"]["expected"]["slots"][0])

    def test_holiday_load_cannot_change_normal_demand_or_window_selection(self):
        target, days, ev, radiation = self.fixture()
        away = {date(2026, 10, day) for day in range(1, 6)}
        before = model.predict(target, days, ev, next(iter(radiation.values())), radiation,
                               excluded_demand_days=away)
        for day in away:
            days[day]["energy_consumed_wh"] = [1e6] * 96
            ev[day] = [1e6] * 96
        after = model.predict(target, days, ev, next(iter(radiation.values())), radiation,
                              excluded_demand_days=away)
        self.assertEqual(before, after)
        self.assertEqual(after["load_model"]["last_day"], "2026-09-30")
        self.assertEqual(after["load_model"]["excluded_demand_dates"], sorted(day.isoformat() for day in away))
        self.assertEqual(after["solar_fit"]["last_day"], "2026-10-05")
        self.assertEqual(after["solar_fit"]["days"], 90)

    def test_holiday_targets_are_excluded_from_error_calibration(self):
        target, days, ev, radiation = self.fixture()
        away = {date(2026, 10, day) for day in range(1, 6)}
        residuals = model.rolling_residuals(days, ev, radiation, before=target, count=2,
                                           excluded_demand_days=away)
        self.assertEqual([row["target_date"] for row in residuals], ["2026-09-29", "2026-09-30"])

    def test_household_calendar_dates_are_inclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            path.write_text(json.dumps({"periods": [{"start_date": "2026-10-01", "end_date": "2026-10-05",
                                                     "exclude_from_normal_demand": True}]}))
            excluded, _ = load_household_calendar(path)
            self.assertEqual(excluded, {date(2026, 10, day) for day in range(1, 6)})

    def test_zero_charging_days_are_included(self):
        target, days, ev, _ = self.fixture()
        samples, info = model.demand_samples(target, days, ev)
        self.assertGreaterEqual(info["sample_days"], 14)
        self.assertEqual(sum(sum(row["ev"]) == 0 for row in samples), len(samples) // 2)

    def test_no_future_meter_or_weather_training_leakage(self):
        target, days, ev, radiation = self.fixture()
        before = model.predict(target, days, ev, next(iter(radiation.values())), radiation)
        days[target] = {field: [1e6] * 96 for field in model.FIELDS}
        ev[target] = [1e6] * 96
        radiation[target] = [1e6] * 24
        after = model.predict(target, days, ev, next(iter(radiation.values())), radiation)
        self.assertEqual(before, after)

    def test_zero_radiation_is_zero_solar(self):
        target, days, ev, radiation = self.fixture()
        result = model.predict(target, days, ev, [0.0] * 24, radiation)
        self.assertEqual(result["scenarios"]["expected"]["daily"]["Produced"], 0)

    def test_hourly_calibration_known_response(self):
        target, days, _, radiation = self.fixture()
        fit = model.fit_solar(days, radiation, target)
        self.assertAlmostEqual(fit["coefficients"][12], 4.0)

    def test_complete_day_validation(self):
        start = datetime(2026, 10, 1, tzinfo=TZ)
        records = [(start + timedelta(minutes=15 * i), "p", 0.0) for i in range(96)]
        valid, _ = complete_interval_days(records, fields=("p",), before=date(2026, 10, 2))
        self.assertEqual(len(valid), 1)
        for invalid in (records[:-1], records + [records[0]], records[:-1] + [(records[-1][0], "p", float("nan"))]):
            result, _ = complete_interval_days(invalid, fields=("p",), before=date(2026, 10, 2))
            self.assertFalse(result)
        result, _ = complete_interval_days(records, fields=("p",), before=date(2026, 10, 1))
        self.assertFalse(result)

    def weather(self):
        start = datetime(2026, 10, 1, tzinfo=TZ)
        return {"timezone": str(TZ), "_retrieved_at": start.isoformat(), "hourly": {
            "time": [(start + timedelta(hours=i)).isoformat() for i in range(25)],
            "shortwave_radiation": list(range(25))}}

    def test_radiation_shift_and_next_midnight(self):
        result = radiation_days(self.weather(), TZ)
        self.assertEqual(result[date(2026, 10, 1)], list(range(1, 25)))

    def test_incomplete_weather_is_rejected(self):
        weather = self.weather()
        weather["hourly"]["shortwave_radiation"][10] = None
        self.assertFalse(radiation_days(weather, TZ))
        weather = self.weather()
        weather["hourly"]["time"].pop()
        self.assertFalse(radiation_days(weather, TZ))

    def test_stale_weather_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            path.write_text(json.dumps(self.weather()))
            now = datetime(2026, 10, 1, 7, tzinfo=TZ)
            values, status, _ = load_weather(now.date(), TZ, now=now, path=path)
            self.assertIsNone(values)
            self.assertEqual(status["reason"], "weather_stale")

    def test_archive_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            first = archive_forecasts({"value": 1}, directory=Path(directory))
            second = archive_forecasts({"value": 2}, directory=Path(directory))
            self.assertNotEqual(first, second)
            self.assertEqual(json.loads(first.read_text())["value"], 1)

    def test_residual_bounds_are_nonnegative_and_ordered(self):
        errors = [{"residual": {"Produced": -10.0}}] * 20
        bounds = model.residual_bounds(1, "Produced", errors, {})
        self.assertEqual(bounds, {"low": 0.0, "high": 1})

    def test_netting_each_load_scenario_retains_grid_variability(self):
        target, days, ev, _ = self.fixture()
        samples, _ = model.demand_samples(target, days, ev)
        result = model.forecast_paths([0.25] * 96, samples)
        row = result["slots"][44]
        self.assertGreater(row["Imported"], max(0, row["Consumed"] - row["Produced"]))

    def test_charging_window_uses_tomorrow_across_midnight(self):
        start = datetime(2026, 10, 6, 23, tzinfo=TZ)
        forecasts = {}
        for offset in (0, 1):
            day = start.date() + timedelta(days=offset)
            rows = [{"Produced": float(offset), "Consumed": 0.0, "HotWaterEstimate": 0.0}] * 96
            forecasts[day] = {"scenarios": {"no_ev": {"slots": rows}}, "weather": {"available": True},
                              "diagnostics": {"solar_fit": {"days": 30}}}
        hours = window_hours(forecasts, start)
        self.assertEqual(hours[0]["rating"], "poor")
        self.assertEqual(hours[1]["rating"], "good")
        self.assertEqual(hours[1]["time"], "2026-10-07T00:00:00+10:00")

    def test_issuance_selection_ignores_unpublished_and_keeps_horizons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for issued, published in (("2026-10-04T09:00:00+10:00", True),
                                       ("2026-10-05T09:00:00+10:00", True),
                                       ("2026-10-05T12:00:00+10:00", True),
                                       ("2026-10-05T15:00:00+10:00", False)):
                path = archive_forecasts({"issued_at": issued, "model_version": model.VERSION,
                                          "forecasts": [{"target_date": "2026-10-06"}]}, directory=root)
                if published:
                    mark_published(path)
            selected = published_forecasts(root, TZ)
            self.assertEqual(len(selected), 2)
            self.assertEqual(selected[(model.VERSION, 1, date(2026, 10, 6))][0].hour, 12)

    def test_changed_archive_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = archive_forecasts({"value": 1}, directory=root)
            mark_published(path)
            path.write_text('{"value":2}')
            with self.assertRaises(ValueError):
                published_forecasts(root, TZ)

    def test_publisher_retains_dashboard_tags_and_emits_version(self):
        target, days, ev, _ = self.fixture()
        result = model.predict(target, days, ev)["scenarios"]["expected"]
        daily = {metric: Forecast(value, value, value, 14) for metric, value in result["daily"].items()}
        forecast = {"target_date": target.isoformat(), "daily_kwh": daily, "weather_adjusted_daily_kwh": daily,
                    "hourly_kwh": result["hourly"], "weather_adjusted_hourly_kwh": result["hourly"],
                    "weather": {"available": False}, "uncertainty": {"lead_time_calibrated": False}}
        with patch.dict("os.environ", {"INFLUXDB_ORG": "test", "INFLUXDB_BUCKET": "test"}), \
                patch("record.history.predict_tomorrow.influx_client") as client:
            self.assertEqual(write_forecasts([forecast]), 200)
            points = client.return_value.write_api.return_value.write.call_args.kwargs["record"]
            lines = [point.to_line_protocol() for point in points]
            self.assertTrue(any("model=weather_adjusted" in line for line in lines))
            self.assertIn(f'model_version="{model.VERSION}"', lines[0])


if __name__ == "__main__":
    unittest.main()
