# Enphase History Import

Imports Enlighten monthly CSV exports into InfluxDB as `enphase_history_15m`.

Expected CSV columns:

- `Date/Time`
- `Energy Produced (Wh)`
- `Energy Consumed (Wh)`
- `Exported to Grid (Wh)`
- `Imported from Grid (Wh)`

Run:

```powershell
docker compose run --rm --no-deps monitor python record/history/import_history.py
```

The importer skips LibreOffice lock files and deduplicates rows by timestamp.

## Hourly EV Charging Export

Export each hour covered by stored IQ EV charger telemetry for battery-storage
analysis:

```powershell
docker compose run --rm --no-deps monitor python record/history/export_ev_charging_hourly_csv.py
```

The CSV includes measured EV charging energy, concurrent site production,
consumption, grid import/export, telemetry completeness, and an inferred charge
mode. The mode is `green_charging`, `grid_assisted_charging`, or `no_charging`.
It is inferred from concurrent whole-site grid import, not read from the IQ2 app
setting. Use `--start-date`, `--end-date`, or `--output` to override the
automatic telemetry coverage range and default output path.
