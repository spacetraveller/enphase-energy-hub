# Gateway information tools

## Inverter identification

After configuring the Gateway host and local token, run:

```powershell
docker compose run --rm --no-deps monitor python record/info/discover_inverters.py
```

This reports the inverter count and writes private serials with editable aliases
to `monitor/record/knowledge/local-solar-layout/inverters.json`. The directory is
ignored by Git. Re-running the command preserves aliases. Do not paste the
inventory into chat or include it in a shared repository. Panel positions and
roof geometry are optional; see `docs/solar-layout-mapping.md`.

## Gateway metadata snapshots

This folder is for one-off `/info` captures. It is intentionally separate from the always-running reachability monitor so firmware/package metadata is only stored when you choose to record it.

Run a snapshot with the Python tool inside the monitor container:

```powershell
docker compose run --rm --no-deps monitor python record/info/record_info.py
```

Snapshots are written to `monitor/record/info/snapshots`.
If the Gateway is reachable from the host but not from Docker, investigate the
container network route; keep Gateway API requests in the repository's Python
tooling.
