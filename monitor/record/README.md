# Monitor data and tools

This directory contains the Python code used by the monitor service and tools
you run on demand. The always-running monitor collects the configured recurring
Gateway and site measurements and writes them to InfluxDB. Scripts under this
directory are generally one-off imports, reports, snapshots, forecast jobs, or
diagnostics; they do not run continuously unless a scheduled workflow invokes
them.

## Where things live

| Path | Purpose |
| --- | --- |
| `history/` | Historical data imports, forecasts, exports, and related documentation. See [`history/README.md`](history/README.md) and [`history/FORECASTING.md`](history/FORECASTING.md). |
| `cloud/` | On-demand Enphase cloud API setup, snapshots, imports, and reports. This is separate from recurring local Gateway polling. See [`cloud/README.md`](cloud/README.md). |
| `debug/` | Manually run diagnostic and analysis scripts. See the [debug script index](debug/README.md). |
| `endpoints/` | Gateway endpoint definitions and endpoint-specific collection code. |
| `network/` | Network reachability checks and related configuration. |
| `cadence/` | Collection timing and cadence support. See [`cadence/README.md`](cadence/README.md). |
| `weather/` | Weather data and forecast support. |
| `info/` | One-off system and equipment information tools. See [`info/README.md`](info/README.md). |
| `knowledge/` | Local reference material and household-specific knowledge. Treat all local contents as private. |
| `inbox/` | Local incoming files awaiting processing. Treat contents as private. |
| `archive/` | Local archived inputs and records. Treat contents as private. |

## Shared support and entry points

- `household_config.py` reads optional household feature settings. Defaults are
  generic, and EV or hot-water assumptions should only be enabled when they fit
  the installation.
- `query_helpers.py` provides shared InfluxDB clients and query helpers, plus
  reusable history interval, timezone, time-label, and percentile utilities.
  Prefer these helpers when adding diagnostics or reports; keep genuinely
  custom Flux query shapes in the calling tool.
- `solar_layout.py` is a command-line tool to initialize, validate, and render
  a local panel layout. Follow the [solar layout mapping guide](../../docs/solar-layout-mapping.md).

## Running tools

Run scripts from the project root inside the Compose `monitor` service so they
use the project's Python dependencies and environment. The usual pattern is:

```powershell
docker compose run --rm --no-deps monitor python record/<path>/<script>.py
```

For commands that need Compose to read `.env` values during variable
interpolation, use:

```powershell
docker compose --env-file .env run --rm --no-deps monitor python record/<path>/<script>.py
```

Replace the placeholder with a documented script path. Check the relevant
folder guide first, especially before importing data or using cloud API tools.
Some tools write output locally or to InfluxDB; read their help and guide before
running them.

## Private local data

Installation-specific files are private and must not be included in shared
branches, archives, examples, or screenshots. This includes historical imports
and exports, snapshots, knowledge files and load signatures, inbox and archive
contents, and solar maps, source roof images, or screenshots. Credentials and
site or device identifiers are private too. Use synthetic examples and aliases
in tracked documentation. Git ignore rules help prevent new files from being
added, but do not remove files already tracked or present in Git history.
