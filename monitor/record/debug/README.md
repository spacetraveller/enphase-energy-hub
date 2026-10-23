# Debug and analysis scripts

This folder contains manually run query, inspection, analysis, and maintenance scripts. It is an index, not a promise that every script is a supported end-user feature; some scripts were created for a particular investigation and may need adaptation.

Run scripts from the project root so Docker Compose can load the project's environment and networking:

```powershell
docker compose run --rm --no-deps monitor python record/debug/<script>.py
```

Replace `<script>.py` with the filename below. Add the script's CLI options after its filename. For example:

```powershell
docker compose run --rm --no-deps monitor python record/debug/query_energy_flow.py --help
```

Some scripts need imported history, particular InfluxDB measurements, or configured services. Check a script's `--help` and its source before use. Review options carefully before running maintenance scripts: some support data deletion or rebuilding. Keep exported and analyzed site data private.

## Query and inspection

- [find_last_day_query.py](find_last_day_query.py) - find daily history totals matching a threshold.
- [inspect_energy_flow_measurement.py](inspect_energy_flow_measurement.py) - inspect the Energy Flow measurement structure and values.
- [list_measurements.py](list_measurements.py) - list measurements available in InfluxDB.
- [measurement_summary.py](measurement_summary.py) - summarize a selected measurement.
- [query_dashboard_solar_today.py](query_dashboard_solar_today.py) - inspect the data used for today's solar dashboard view.
- [query_energy_flow.py](query_energy_flow.py) - query recent Energy Flow rows with reusable filters.
- [query_energy_flow_w_now.py](query_energy_flow_w_now.py) - inspect current-power Energy Flow fields.
- [query_ev_charger_telemetry.py](query_ev_charger_telemetry.py) - summarize stored EV charger telemetry.
- [query_history_source_overlap.py](query_history_source_overlap.py) - look for overlapping imported history sources.
- [query_history_summary.py](query_history_summary.py) - summarize imported history data.
- [query_meter_fields.py](query_meter_fields.py) - query recent meter fields with optional filters.
- [query_meter_series_summary.py](query_meter_series_summary.py) - summarize meter time series.
- [query_measurement_cadence.py](query_measurement_cadence.py) - inspect measurement sampling cadence.
- [query_net_consumption.py](query_net_consumption.py) - inspect net-consumption history.
- [query_reachability.py](query_reachability.py) - inspect recorded Gateway reachability data.
- [query_solar_today_sources.py](query_solar_today_sources.py) - compare data sources for today's solar production.
- [query_voltage.py](query_voltage.py) - inspect voltage readings.
- [verify_grafana_dashboard.py](verify_grafana_dashboard.py) - check a dashboard definition, optionally against a live Grafana instance.

## Solar and history analysis

- [analyze_inverter_shading.py](analyze_inverter_shading.py) - compare inverter output over selected time windows; can use a local solar layout map.
- [analyze_overnight_baseload_noise.py](analyze_overnight_baseload_noise.py) - summarize overnight household baseload variation.
- [analyze_recent_load_signature.py](analyze_recent_load_signature.py) - analyze a recent short load change in Energy Flow samples.
- [analyze_solar_time_window.py](analyze_solar_time_window.py) - rank historical solar production for a local time-of-day window.
- [compare_daily_solar_estimates.py](compare_daily_solar_estimates.py) - compare solar production estimates for a local day.
- [compare_historical_weather_years.py](compare_historical_weather_years.py) - compare historical solar weather for matched years.
- [compare_history_months.py](compare_history_months.py) - compare two calendar months of imported history.
- [compare_same_time_recent.py](compare_same_time_recent.py) - compare recent values at similar times of day.
- [compare_solar_day_segments.py](compare_solar_day_segments.py) - compare matched solar-production segments between years.
- [compare_solar_today_sources.py](compare_solar_today_sources.py) - compare today's solar production sources.
- [compare_winter_solar_years.py](compare_winter_solar_years.py) - compare winter solar production between years.
- [monthly_peak_solar_times.py](monthly_peak_solar_times.py) - summarize when daily solar peaks occur by month.
- [solar_today_percentile.py](solar_today_percentile.py) - compare today's solar output with historical values.
- [summarize_day_energy.py](summarize_day_energy.py) - summarize production, consumption, grid flow, and high-load periods for a local day.

## Forecasting and EV

- [analyze_ev_energy_sources.py](analyze_ev_energy_sources.py) - estimate EV charging energy from aligned site telemetry.
- [analyze_grid_assist_afternoon.py](analyze_grid_assist_afternoon.py) - analyze afternoon grid assistance alongside site production and demand.
- [assess_ev_charging_window.py](assess_ev_charging_window.py) - assess a potential solar EV charging window.
- [audit_forecasts.py](audit_forecasts.py) - audit forecast values against selected historical ranges.
- [compare_forecast_to_actual.py](compare_forecast_to_actual.py) - compare stored forecasts with observed production.
- [evaluate_ev_charge_session.py](evaluate_ev_charge_session.py) - evaluate a charging session using charger telemetry and meter deltas.
- [evaluate_forecasts.py](evaluate_forecasts.py) - evaluate forecast performance over a selected period.

EV-related tools need compatible EV telemetry or relevant site history; they do not imply that EV features are enabled for every installation.

## Data repair and maintenance

- [check_energy_flow_writes.py](check_energy_flow_writes.py) - inspect Energy Flow write behavior and possible conflicts.
- [find_write_conflict.py](find_write_conflict.py) - investigate conflicting writes.
- [delete_measurement.py](delete_measurement.py) - delete a measurement; destructive operation, inspect the script and arguments first.
- [repair_energy_flow_measurement.py](repair_energy_flow_measurement.py) - export, normalize, and optionally rebuild Energy Flow data. Read its options carefully; execution can delete and rewrite stored points.

Before any repair or deletion, understand the selected time range, target measurement, and backup/export behavior. These tools are for deliberate maintenance, not routine setup.
