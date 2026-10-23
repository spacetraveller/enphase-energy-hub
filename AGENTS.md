# AI Agent and Contributor Guide

Start with [README.md](README.md) for the project overview and dashboard descriptions. For user installation and configuration, use [docs/installation.md](docs/installation.md). Read the documentation for the relevant subsystem before changing it. Keep credentials and site-specific files private.

## Orientation and privacy

- Treat `.env`, imported history, archive/export folders, calendars, knowledge files, serials, site IDs, screenshots, and solar-layout files as private. Do not print secrets, add real household/site records to fixtures or docs, or stage private files.
- Ignore rules do not remove files already tracked or present in Git history. Review `git status` and the staged diff before preparing a shareable commit.
- Do not assume a user's OS, shell, network, Gateway address, utility tariff, devices, schedule, or timezone from another installation. Use generic defaults and ask only when a decision depends on missing facts.
- Do not modify Grafana dashboard JSON unless the user approves the specific dashboard change.
- Do not run or add tests unless the user asks for testing or verification.

## Query and diagnostic code

- Prefer reading existing project scripts and dashboard queries before inventing commands.
- Before adding InfluxDB/history diagnostics, check `monitor/record/query_helpers.py`. Prefer its `query_measurement_values(...)`, `query_measurement_rows(...)`, `latest_measurement_rows(...)`, `first_last_delta(...)`, `query_history_records(...)`, `complete_interval_days(...)`, and `query_history_field_by_day_slot(...)` helpers. Reuse its timezone, interval, time-label, and percentile utilities where their semantics match the caller. Put reusable query behavior there and keep diagnostic scripts as small CLI/report wrappers.
- When a genuinely custom Flux query is needed (for example `pivot`, `map`, `aggregateWindow`, or a multi-measurement union), reuse `influx_client()`, `influx_bucket()`, `influx_org()`, `flux_time()`, and `query_rows()` from `query_helpers.py`.
- Follow the existing Docker import pattern for diagnostics:

  ```python
  import sys
  from pathlib import Path

  sys.path.append(str(Path(__file__).resolve().parents[2]))
  from record.query_helpers import query_measurement_rows
  ```

- Avoid fragile `python -c`, deeply quoted PowerShell, pipelines, and clever inline shell composition. Put reusable or awkward logic in a script under `monitor/record/debug/` and run it in Docker.
- For read-only InfluxDB checks, prefer shared helpers or the CLI in the InfluxDB container. Never print or pass the InfluxDB token in output.

## Enphase, Docker, and data handling

- Use Python tooling in this repository for all Enphase HTTP/API access. Never send Enphase requests from PowerShell. Keep local Gateway and cloud API tokens only in `.env`.
- Run Python scripts inside the existing `monitor` image so dependencies and networking match:

  ```powershell
  docker compose run --rm --no-deps monitor python <script>
  ```

  For commands needing Compose interpolation, use `docker compose --env-file .env run --rm --no-deps monitor python <script>`. When outside the repository directory, pass `-f <absolute-path-to-project>/docker-compose.yml`.
- Keep the always-running monitor focused on recurring metrics. Use one-off snapshots for static metadata. Do not create replacement measurements such as `*_v2` for bad data without explicit approval; investigate and propose repair of the existing measurement first.
- Production/API collection depends on `PRODUCTION_MONITORING_ENABLED`, endpoint flags, and a valid local Gateway token. If the Gateway is unstable, disable authenticated polling and retain only reachability checks while investigating.
- During setup, identify the inverters with `docker compose run --rm --no-deps monitor python record/info/discover_inverters.py`. This writes serials and editable aliases only to the ignored `monitor/record/knowledge/local-solar-layout/inverters.json`; report the count and any access error without printing serials or copying the file into chat. Preserve user-edited aliases on rediscovery. Compare the count with the user's Enphase app or installer records when possible. A zero count needs investigation but must not block aggregate monitoring.
- Offer optional panel-to-inverter mapping after identification. Ask whether the user wants to provide panel positions, roof-face directions, inclinations, and a local roof image or screenshot. Keep those details private; do not require them to finish setup. Never infer physical panel positions from serial order or proximity. Explain that current forecasts do not yet consume per-panel positions or inclinations.
- Treat intermittent reachability failures as events to investigate, not proof of any single cause. Compare hostnames and IPs with repeatable Python probes recording success, latency, timeout/error type, and the same endpoint/authentication mode. Short runs are indicative only.
- The reachability dashboard measures the local landing-page response/redirect, not authenticated production collection. If Docker cannot reach the landing page but the host can, consider `ENPHASE_REACHABILITY_PATH` and `ENPHASE_REACHABILITY_TARGET`; keep reachability and authenticated production/API signals separate.

## Forecasts and dashboards

- For advice depending on current solar forecasts, refresh weather data and regenerate predictions first. `record/history/predict_tomorrow.py` normally starts at tomorrow; use `PREDICTION_FIRST_OFFSET_DAYS=0` to replace today's point and `PREDICTION_FORECAST_DAYS=4` for today plus the normal three forward days.
- For historical solar time-of-day analysis, use `record/debug/analyze_solar_time_window.py` with imported `enphase_history_15m` data. Verify stored forecast values with an existing comparison helper if Grafana appears stale; do not assume a script run changed a panel.
- Before changing forecasting assumptions, inspect weather summary fields such as total and expected-month tilted irradiance, irradiance ratio, cloud cover, rain probability, and production multiplier.
- Dashboard JSON edits require approval for the specific change. Suggestions and read-only review are fine. If approved to edit dashboard JSON, keep rendered files generated through the project's provisioning workflow and observe the LF line-ending requirement for `grafana/render-dashboards.sh`.

## Shell and communication

- Use the project root as the working directory, especially for Docker Compose commands. If a file is unexpectedly missing, verify the working directory/path before concluding it is absent.
- On Windows, prefer quoted absolute paths and `-LiteralPath` for file operations when the current directory is uncertain. Avoid relying on `Set-Location` for command correctness.
- Do not repeat a failing command shape without identifying whether the issue is path, quoting, Docker permissions, network, or data related.
- Report changes concisely with what changed and how it was checked. Never claim tests were run when they were not.
