# Envoy Endpoint Exploration

This folder contains read-only probes for local Envoy endpoints. The script uses `ENPHASE_ACCESS_TOKEN` from `.env` as a Bearer token and records only endpoint statuses plus short response summaries.

The Python version is preferred because it uses OpenSSL inside the container. Override `ENPHASE_HOST` with the Envoy IPv4 address if Docker cannot reach `envoy.local` over IPv6:

```powershell
docker compose run --rm --no-deps -e ENPHASE_HOST=192.168.1.100 -e ENPHASE_REQUEST_HOST=envoy.local monitor python record/endpoints/explore_endpoints.py
```

The PowerShell version can also check unauthenticated HTTP behavior:

```powershell
powershell -ExecutionPolicy Bypass -File .\monitor\record\endpoints\explore_endpoints.ps1
```

Snapshots are written to `monitor/record/endpoints/snapshots`.
