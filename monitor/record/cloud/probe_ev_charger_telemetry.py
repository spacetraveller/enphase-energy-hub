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
    serials = [serial.strip() for serial in env_required("ENPHASE_CLOUD_EVSE_SERIALS").split(",") if serial.strip()]
    serial = os.getenv("ENPHASE_CLOUD_EVSE_SERIAL", serials[0])
    local_tz = ZoneInfo(env_timezone("ENPHASE_HISTORY_TIMEZONE", "HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))
    day = os.getenv("ENPHASE_CLOUD_PROBE_DAY", datetime.now(local_tz).date().isoformat())
    start = datetime.fromisoformat(day).replace(tzinfo=local_tz)
    path = f"/systems/{system_id}/ev_charger/{serial}/telemetry"
    params = {
        "start_at": str(int(start.timestamp())),
        "granularity": os.getenv("ENPHASE_CLOUD_PROBE_GRANULARITY", "day"),
    }
    response = cloud_get(path, params=params)
    result = {
        "path": path,
        "params": params,
        "status_code": response.status_code,
        "ok": 200 <= response.status_code < 300,
        "serial_no": serial,
    }
    try:
        payload = response.json()
        consumption = payload.get("consumption") if isinstance(payload, dict) else None
        result["consumption_count"] = len(consumption) if isinstance(consumption, list) else None
        result["consumption_wh"] = (
            sum(float(item.get("consumption", 0.0)) for item in consumption if isinstance(item, dict))
            if isinstance(consumption, list)
            else None
        )
        result["summary"] = compact(redact(payload))
    except ValueError:
        result["summary"] = response.text[:1000]

    print(
        f"{path} -> {response.status_code} "
        f"consumption_count={result.get('consumption_count')} "
        f"consumption_wh={result.get('consumption_wh')}"
    )
    output_path, latest_path = write_json_snapshot("ev_charger_telemetry_probe", result)
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
