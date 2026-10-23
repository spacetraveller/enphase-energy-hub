import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cloud_common import cloud_get, compact, env_required, redact, write_json_snapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.query_helpers import env_timezone

def main() -> None:
    system_id = env_required("ENPHASE_CLOUD_SYSTEM_ID")
    local_tz = ZoneInfo(env_timezone("ENPHASE_HISTORY_TIMEZONE", "HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
    default_day = (datetime.now(local_tz).date() - timedelta(days=1)).isoformat()
    day = os.getenv("ENPHASE_CLOUD_PROBE_DAY") or default_day
    start = datetime.fromisoformat(day).replace(tzinfo=local_tz)
    end = start + timedelta(days=1)
    path = os.getenv(
        "ENPHASE_CLOUD_PROBE_PATH",
        f"/systems/{system_id}/telemetry/production_meter",
    )
    params = {
        "start_at": str(int(start.timestamp())),
        "end_at": str(int(end.timestamp())),
    }
    granularity = os.getenv("ENPHASE_CLOUD_PROBE_GRANULARITY", "")
    if granularity:
        params["granularity"] = granularity
    response = cloud_get(path, params=params)
    result = {
        "path": path,
        "params": params,
        "status_code": response.status_code,
        "ok": 200 <= response.status_code < 300,
    }
    try:
        payload = response.json()
        intervals = payload.get("intervals") if isinstance(payload, dict) else None
        result["interval_count"] = len(intervals) if isinstance(intervals, list) else None
        result["summary"] = compact(redact(payload))
    except ValueError:
        result["summary"] = response.text[:1000]
    print(f"{path} -> {response.status_code} intervals={result.get('interval_count')}")
    print(result["summary"])
    output_path, latest_path = write_json_snapshot("telemetry_probe", result)
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
