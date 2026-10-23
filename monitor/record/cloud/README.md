# Enphase Enlighten API v4

This folder contains one-off tools for the Enphase cloud API. They are for
endpoint exploration, OAuth setup, and token refresh. They are intentionally
separate from the always-running local Envoy monitor.

## Watt Plan Notes

The Watt plan is useful for conservative cloud snapshots, not high-cadence
monitoring. As of the current Enphase developer docs, Watt allows:

- 10 hits per minute
- 1000 hits per month
- System Details
- Site level Production Monitoring
- Site level Consumption Monitoring
- EV Charger Monitoring

Use local Envoy/Gateway polling for recurring high-frequency dashboard data.
Use cloud API snapshots for data that is unavailable locally, slower cadence
checks, endpoint discovery, and EV charger metadata/telemetry experiments.

Cloud requests go through `cloud_common.cloud_get()`, which enforces a shared
per-process minimum delay before each Enphase API GET. The default is controlled
by `ENPHASE_CLOUD_MIN_REQUEST_INTERVAL_SECONDS`, falling back to
`ENPHASE_CLOUD_REQUEST_DELAY_SECONDS`, then `8.0` seconds. At the Watt limit of
10 hits/minute, keep this at `8` seconds or higher so a backfill stays below the
80% warning threshold. The helper also retries `429 Too Many Requests` responses
using `Retry-After` or `X-RateLimit-Reset` when Enphase returns those headers.

Monthly limits are much tighter than minute limits. On the Watt plan, one full
May site-history backfill can consume 4 cloud hits per local day, while EV
charger telemetry consumes 1 cloud hit per local day. Prefer narrow date ranges,
avoid repeated dry-runs against the same range, and use local InfluxDB data for
analysis once imported.

`cloud_get()` also appends one local JSONL ledger entry per Enphase cloud GET to
`monitor/record/cloud/usage/api_calls.jsonl` by default. The ledger is ignored by
git and contains timestamps, path names, status codes, elapsed time, retry
attempts, and rate-limit response headers. It does not store tokens or API keys.
Summarize current month, last 30 days, and last minute usage with:

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/report_api_usage.py
```

Controls:

- `ENPHASE_CLOUD_USAGE_LEDGER_ENABLED=false` disables local usage logging.
- `ENPHASE_CLOUD_USAGE_LEDGER=/path/to/api_calls.jsonl` changes the ledger path.
- `ENPHASE_CLOUD_MONTHLY_LIMIT=1000` and `ENPHASE_CLOUD_MINUTE_LIMIT=10` tune the report limits.

## OAuth Flow

Enphase API v4 uses OAuth 2.0. API requests need:

- `Authorization: Bearer <access token>`
- `key: <api key>`

The authorization URL is the URL the system owner opens to approve the app.
Append the same `redirect_uri` you will use when exchanging the authorization
code. The default redirect URI from Enphase is:

```text
https://api.enphaseenergy.com/oauth/redirect_uri
```

After approval, copy the `code` from the redirect page/URL into `.env` as
`ENPHASE_CLOUD_AUTH_CODE`, then exchange it for tokens.

## Setup

Add these values to `.env`:

```text
ENPHASE_CLOUD_AUTHORIZATION_URL=https://api.enphaseenergy.com/oauth/authorize?response_type=code&client_id=...
ENPHASE_CLOUD_CLIENT_ID=...
ENPHASE_CLOUD_CLIENT_SECRET=...
ENPHASE_CLOUD_API_KEY=...
ENPHASE_CLOUD_REDIRECT_URI=https://api.enphaseenergy.com/oauth/redirect_uri
```

Build the final authorization URL:

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/build_authorization_url.py
```

After approval, add the returned code to `.env`:

```text
ENPHASE_CLOUD_AUTH_CODE=...
```

Exchange the code for tokens. Mount the gitignored root `.env` into the
one-off container so the command updates its cloud token fields without
printing token values or changing other settings:

```powershell
docker compose --env-file .env run --rm --no-deps -v ".\.env:/app/.env" monitor python record/cloud/exchange_oauth_code.py --write-env /app/.env
```
Access tokens last about one day; refresh tokens last about one month for
Watt/Kilowatt/Megawatt apps.

Refresh tokens later with:

```powershell
docker compose --env-file .env run --rm --no-deps -v ".\.env:/app/.env" monitor python record/cloud/refresh_oauth_token.py --write-env /app/.env
```

## Endpoint Snapshots

Run a conservative endpoint snapshot:

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/explore_api_v4.py
```

Snapshots are written to `monitor/record/cloud/snapshots/` and ignored by git.
They include status codes, response shape, selected response headers, and a
sanitized/compact payload preview.

## IQ EV Charger Discovery

The IQ EV charger serial can appear in:

```text
/api/v4/systems/{system_id}/summary
```

Observed response location:

```text
evse_power[0].{serial_number}
```

The response can expose EVSE min/max charging power under a key matching the
charger's serial number. Use the serial reported for your own system.

Store it in `.env`:

```text
ENPHASE_CLOUD_SYSTEM_ID=your_enphase_system_id
ENPHASE_CLOUD_EVSE_SERIALS=your_ev_charger_serial
```

Current finding: older/guessed serial-specific paths such as
`/api/v4/systems/{system_id}/{serial_no}/evse_lifetime` and
`/api/v4/systems/{system_id}/{serial_no}/evse_telemetry` returned `401`.

The documented EV charger telemetry endpoint works:

```text
/api/v4/systems/{system_id}/ev_charger/{serial_no}/telemetry
```

It accepts `start_at` and `granularity` and returns EV charger `consumption`
intervals. Probe it with:

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/probe_ev_charger_telemetry.py
```

Import a date range into InfluxDB measurement `enphase_ev_charger_telemetry`:
Replace `YYYY-MM-DD` with the inclusive start and end dates for your import.

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/import_ev_charger_telemetry.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --execute
```

This uses one `granularity=day` request per local day and writes
15-minute `consumption_wh` points using the interval start timestamp, matching
the imported history convention. Each completed day is written to InfluxDB
immediately, so a later request failure does not discard earlier successful
days. Batch importers stop immediately on any non-success response. In
particular, a `401` stops the date loop after the first failed request so an
expired token cannot consume the remaining request budget.

If the cloud API returns `401`, refresh the OAuth tokens in the root `.env`,
then run the import normally:

```powershell
docker compose --env-file .env run --rm --no-deps -v ".\.env:/app/.env" monitor python record/cloud/refresh_oauth_token.py --write-env /app/.env
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/import_ev_charger_telemetry.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --execute
```

Evaluate an EV charging session against whole-site import/export meter deltas:

```powershell
docker compose run --rm --no-deps monitor python record/debug/evaluate_ev_charge_session.py --date YYYY-MM-DD --stop HH:MM
```

The diagnostics discover production and net meter EIDs from recent InfluxDB
readings. If the meter layout cannot be inferred uniquely, set
`METER_PRODUCTION_EID` and/or `METER_NET_EID` in the private root `.env`; do
not commit those installation-specific values.

The evaluator sums imported EVSE `consumption_wh`, compares it with site import
over the same window, estimates solar-assisted EV energy, and reports pure-grid
cost, actual import cost, foregone feed-in credit, and effective cost per EV kWh.

Analyse whether grid-assisted afternoon charging is worthwhile for this time of
year:

```powershell
docker compose run --rm --no-deps monitor python record/debug/analyze_grid_assist_afternoon.py
```

Defaults are illustrative and should be set to match the charger and household.
Override with `GRID_ASSIST_CHARGER_KW`, `GRID_ASSIST_START_HOUR`,
`GRID_ASSIST_START_MINUTE`, `GRID_ASSIST_MONTHS`,
`IMPORT_RATE_AUD_PER_KWH`, or `FEED_IN_RATE_AUD_PER_KWH`.

## Historical Analysis Matching

The local `Enphase Historical Analysis` dashboard is based on imported
15-minute Enlighten CSV data in the `enphase_history_15m` measurement:

- `energy_produced_wh`
- `energy_consumed_wh`
- `exported_to_grid_wh`
- `imported_from_grid_wh`

For daily total comparisons, the lifetime endpoints are low-volume and useful:

- `/api/v4/systems/{system_id}/energy_lifetime`
- `/api/v4/systems/{system_id}/consumption_lifetime`
- `/api/v4/systems/{system_id}/energy_export_lifetime`
- `/api/v4/systems/{system_id}/energy_import_lifetime`

For direct comparison with the imported 15-minute history, the telemetry
endpoints are the better fit:

- `/api/v4/systems/{system_id}/telemetry/production_meter`
- `/api/v4/systems/{system_id}/telemetry/consumption_meter`
- `/api/v4/systems/{system_id}/energy_export_telemetry`
- `/api/v4/systems/{system_id}/energy_import_telemetry`

Observed telemetry request parameters:

- `start_at`: local-day start as epoch seconds
- `granularity`: string value controlling the returned window
- `end_at`: next local midnight as epoch seconds when not using `granularity`

Documented granularity behavior:

- `15mins` returns at most 1 interval
- `day` returns at most 96 intervals, each 15 minutes long

For Historical Analysis backfill, use `granularity=day` and one request per
local day per telemetry endpoint unless a larger documented granularity is
confirmed for the endpoint.
The backfill writes each completed day to InfluxDB after all four telemetry
endpoints have succeeded, avoiding partial energy rows while preserving earlier
days if a later cloud request fails.

Observed response fields:

- production meter intervals use `wh_del`
- consumption meter intervals use `enwh`
- export telemetry intervals are nested and use `wh_exported`
- import telemetry intervals are nested and use `wh_imported`

Compare the most recent imported history day against cloud telemetry:

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/compare_history_telemetry_day.py
```

Compare the same day against daily lifetime endpoints:

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/cloud/compare_history_day.py
```

Compare complete local days. Results may differ by measurement source and interval
alignment; the comparison scripts report the values observed for the selected day.
