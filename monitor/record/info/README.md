# Envoy Info Snapshots

This folder is for one-off `/info` captures. It is intentionally separate from the always-running reachability monitor so firmware/package metadata is only stored when you choose to record it.

Run a snapshot with:

```powershell
.\monitor\record\info\record_info.ps1
```

Snapshots are written to `monitor/record/info/snapshots`.

There is also a Python version at `record_info.py` for container-based use, but the PowerShell script is preferred on this network because the Envoy `/info` endpoint is currently reachable from Windows over IPv6 while Docker may not have IPv6 LAN access.
