# Household energy forecasting

Model version: `interval-empirical-3` (optional household features).

## Normal operation

```powershell
docker compose run --rm --no-deps monitor python record/history/predict_tomorrow.py
```

This refreshes Open-Meteo weather, updates the historical weather cache once per
local day, validates inputs, and publishes tomorrow plus two further days. It uses
the existing prediction measurements and dashboard tags. No background schedule
is installed by this change; refreshes run when this command is invoked.

To replace today's forecast too:

```powershell
docker compose run --rm --no-deps -e PREDICTION_FIRST_OFFSET_DAYS=0 -e PREDICTION_FORECAST_DAYS=4 monitor python record/history/predict_tomorrow.py
```

`--dry-run` computes without publishing predictions or creating an issuance archive
(weather refresh/cache writes still occur). Add `--no-refresh-weather` for a fully
read-only run. `--full-output` includes interval series and scenarios.

## Inputs and quality

- History is source-deduplicated, preferring Enlighten CSV export over cloud data.
  Only closed local days with 96 aligned, finite, nonnegative intervals for all
  four energy fields enter training. Zero-energy intervals/days are valid.
- Household data more than three days stale prevents publication. Invalid or
  incomplete intervals are excluded from training; source records are not changed.
- Forecast radiation is read from the latest complete weather snapshot, avoiding
  mixtures of new and previously stored fields. Data older than six hours or a
  target missing any hourly radiation interval is rejected.
- Open-Meteo radiation represents the **preceding hour**. The value at 11:00 now
  drives 10:00-11:00. The request includes the next midnight needed to close the
  final target day. Cloud cover remains an instantaneous diagnostic.
- A weather or calibration failure stops publication unless the operator explicitly
  selects `--allow-historical-fallback`. Debug callers expose fallback diagnostics.

## Solar

A rolling fit uses up to 90 earlier days of measured AC generation and matching
Open-Meteo horizontal irradiance (GHI). Each hour has its own nonnegative response
coefficient, capturing the combined array response and typical shading.
At least 28 matched days are required. Historical quarter-hour shares divide each
predicted hourly energy total; these are **not** 15-minute weather observations.

The arbitrary cloud/rain penalties and 85/15 solar-to-grid allocation are removed.
Weather already affects irradiance. Static monthly irradiance references remain
only as dashboard diagnostics and do not set production.

The fit describes the combined array response; it is not a physical per-roof
solar simulation and does not require a panel layout. Use the optional local
panel map workflow in [`docs/solar-layout-mapping.md`](../../../docs/solar-layout-mapping.md)
to document panel positions and inverter aliases without adding site data to Git.
The dashboard's legacy GTI field names contain horizontal GHI; diagnostics
identify this input explicitly.

## Household, hot water and EV

The demand model uses recent, complete household history with recency and weekday
weighting. It selects a 14-, 28- or 56-day history window from earlier rolling
consumption errors. A short window with fewer than 14 valid days falls back to
56 days. An optional `HOUSEHOLD_EV_HISTORY_START_DATE` can limit training to the
current household regime.

An optional private household calendar can exclude unusual dates from demand
training and evaluation. Configure `HOUSEHOLD_CALENDAR_PATH` to point to a local
JSON file; when the file is absent, no dates are excluded. Calendar contents are
copied into local forecast archives, so those archives must also stay private.

EV scenarios are optional. Set `HOUSEHOLD_EV_ENABLED=true` for a home with an EV;
set `EV_CHARGER_TELEMETRY_ENABLED=true` only when charger data is available.
With at least 14 complete paired days, EV use can be separated from whole-site
load and historical no-EV/charging-day scenarios can be produced. Missing
telemetry is never interpreted as zero; without sufficient paired data, the
whole-site forecast continues and EV recommendations are marked unavailable.
Scenarios describe past behaviour, not scheduled future charging.

Optional hot-water attribution uses the paired
`HOUSEHOLD_HOT_WATER_ESTIMATE_START` and
`HOUSEHOLD_HOT_WATER_ESTIMATE_END` local times. If not configured, no hot-water
component is inferred. When configured, it is an unconfirmed residual estimate
within the selected interval, not an extra load added to the measured demand.

## Grid flows and uncertainty

Each plausible demand day is netted against predicted solar every 15 minutes:
`export=max(production-consumption,0)`, `import=max(consumption-production,0)`.
These flows are averaged across demand days, then summed into hours and days.
This preserves `production + import = consumption + export` at every resolution.
Expected import and export can both be positive within one interval because they
average different plausible demand outcomes. No individual scenario imports and
exports simultaneously. This avoids understating grid use by netting only a
smoothed household profile.

Daily low/high values initially use rolling retrospective p20/p80 errors. They
are marginal ranges with a nominal 60% coverage, not guaranteed limits and not
energy-balanced alternative scenarios. Until issued forecasts accumulate, these
ranges use reconstructed near-time weather and omit some future-weather error.
The output explicitly states `lead_time_calibrated=false`.

After at least 14 completed forecasts exist for the same model version and local
calendar lead day, bounds automatically use those actual issued-forecast errors,
up to the most recent 28 observations. Lead-day 1, 2 and 3 remain separate.

## Archive and evaluation

Each publication creates a unique, exclusively created JSON file under
`history/forecast_archive/`, with issuance time, version, forecast horizon,
weather input, fitted parameters, quality metadata and all scenario predictions.
An integrity-checked `.published` receipt records successful InfluxDB publication.
Dry runs and failed writes are excluded from scoring. Outputs use full precision;
the dashboard measurements continue to hold the latest forecast for each target.
Archives and weather caches are local data excluded from Git; retain them for
future scoring and include them in any local backup policy.

```powershell
docker compose run --rm --no-deps monitor python record/debug/evaluate_forecasts.py --retrospective
docker compose run --rm --no-deps monitor python record/debug/evaluate_forecasts.py --retrospective --before 2025-01-01 --days 28
docker compose run --rm --no-deps monitor python -m unittest record.history.test_forecast_model -v
```

Evaluation chooses the latest successful issuance per target, model version and
calendar lead day, preventing repeated refreshes from overweighting a date.
`--retrospective` also reports rolling meter-data holdouts using reconstructed
historical weather. That check is distinct from genuine issued forecast scoring.

Evaluate the model against the installation's own history before relying on its
uncertainty ranges. Retrospective checks use reconstructed weather and are not a
substitute for scoring archived forecasts issued one to three days ahead. Small
samples do not establish future forecast calibration.

Weather definitions and archive limitations:
[Open-Meteo forecast documentation](https://open-meteo.com/en/docs) and
[historical forecast documentation](https://open-meteo.com/en/docs/historical-forecast-api).
