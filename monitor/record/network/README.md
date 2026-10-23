# Envoy Network Route Tests

Use this Python-only probe to compare `envoy.local` with the direct Envoy IP address against the same authenticated endpoint.

Run through Docker Compose:

```powershell
docker compose run --rm --no-deps monitor python record/network/compare_routes.py
```

Useful environment overrides:

- `ROUTE_TEST_COUNT=20`
- `ROUTE_TEST_DELAY_SECONDS=5`
- `ROUTE_TEST_TIMEOUT_SECONDS=15`
- `ENPHASE_ROUTE_TEST_PATH=/auth/check_jwt`

Snapshots are written to `monitor/record/network/snapshots`.
