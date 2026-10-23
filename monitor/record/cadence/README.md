# Production Update Cadence

This one-off Python probe samples `/production.json` to estimate how often the Envoy updates values.

It will not poll faster than every 5 seconds.

Run:

```powershell
docker compose run --rm --no-deps -e CADENCE_TEST_INTERVAL_SECONDS=5 -e CADENCE_TEST_SAMPLES=36 monitor python record/cadence/sample_production_cadence.py
```

Snapshots are written to `monitor/record/cadence/snapshots`.
