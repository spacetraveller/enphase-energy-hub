import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


DEFAULT_BASE_URL = "https://api.enphaseenergy.com/api/v4"
SNAPSHOT_KEYS = {
    "system_id",
    "name",
    "public_name",
    "timezone",
    "status",
    "last_report_at",
    "serial_num",
    "serialNumber",
    "product_name",
    "productName",
    "model",
    "modelName",
    "part_num",
    "sku",
    "device_type",
    "type",
    "last_reported_at",
    "lastReportedAt",
    "evse_charge_w",
    "evse_discharge_w",
    "evse_operational_mode",
    "operational_mode",
    "power",
    "end_at",
    "consumption",
}


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def cloud_config() -> dict[str, str]:
    required = ["ENPHASE_CLOUD_API_KEY", "ENPHASE_CLOUD_ACCESS_TOKEN"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing cloud API credentials: "
            + ", ".join(missing)
            + ". Add them to .env and run with docker compose --env-file .env run ..."
        )
    return {
        "base_url": os.getenv("ENPHASE_CLOUD_API_BASE", DEFAULT_BASE_URL).rstrip("/"),
        "api_key": env_required("ENPHASE_CLOUD_API_KEY"),
        "access_token": env_required("ENPHASE_CLOUD_ACCESS_TOKEN"),
        "system_id": os.getenv("ENPHASE_CLOUD_SYSTEM_ID", ""),
        "serials": os.getenv("ENPHASE_CLOUD_EVSE_SERIALS", ""),
    }


def compact(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if key in SNAPSHOT_KEYS or "evse" in lowered or "charger" in lowered:
                result[key] = compact(child, depth + 1)
        if result:
            return result
        return {key: compact(value[key], depth + 1) for key in list(value)[:8]}
    if isinstance(value, list):
        return [compact(item, depth + 1) for item in value[:5]]
    return value


def json_shape(value: Any) -> str:
    if isinstance(value, dict):
        keys = list(value.keys())
        return f"object[{len(keys)}] keys={keys[:12]}"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    return type(value).__name__


def request_json(config: dict[str, str], path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    query = {"key": config["api_key"]}
    if params:
        query.update(params)
    url = f"{config['base_url']}{path}?{urlencode(query)}"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {config['access_token']}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    result: dict[str, Any] = {
        "path": path,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "ok": 200 <= response.status_code < 300,
    }
    try:
        payload = response.json()
        result["shape"] = json_shape(payload)
        result["summary"] = compact(payload)
    except json.JSONDecodeError:
        result["shape"] = "text"
        result["summary"] = response.text[:500]
    return result


def find_evse_serials(payload: Any) -> list[str]:
    serials: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            text = " ".join(str(item).lower() for item in value.values() if not isinstance(item, (dict, list)))
            if "evse" in text or "charger" in text:
                serial = (
                    value.get("serial_num")
                    or value.get("serialNumber")
                    or value.get("serial_no")
                    or value.get("sn")
                )
                if serial:
                    serials.append(str(serial))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return sorted(set(serials))


def raw_summary(result: dict[str, Any]) -> Any:
    return result.get("summary")


def main() -> None:
    config = cloud_config()
    captured_at = datetime.now(timezone.utc)
    results = []

    if config["system_id"]:
        system_ids = [config["system_id"]]
    else:
        result = request_json(config, "/systems")
        results.append(result)
        print(f"/systems -> {result['status_code']} {result['shape']}", flush=True)
        system_ids = []
        summary = raw_summary(result)
        if isinstance(summary, dict):
            systems = summary.get("systems") or summary.get("items") or []
            if isinstance(systems, list):
                for item in systems:
                    if isinstance(item, dict) and item.get("system_id"):
                        system_ids.append(str(item["system_id"]))

    for system_id in system_ids[:3]:
        for path in (
            f"/systems/{system_id}/summary",
            f"/systems/{system_id}/devices",
            f"/systems/{system_id}/latest_telemetry",
        ):
            result = request_json(config, path)
            results.append(result)
            print(f"{path} -> {result['status_code']} {result['shape']}", flush=True)

        serials = [
            serial.strip()
            for serial in config["serials"].split(",")
            if serial.strip()
        ]
        if not serials:
            for result in results:
                if result["path"] == f"/systems/{system_id}/devices":
                    serials = find_evse_serials(result.get("summary"))

        end_at = datetime.now(timezone.utc)
        start_at = end_at - timedelta(hours=24)
        for serial in serials:
            for path, params in (
                (f"/systems/{system_id}/{serial}/evse_lifetime", None),
                (
                    f"/systems/{system_id}/{serial}/evse_telemetry",
                    {
                        "start_at": start_at.isoformat().replace("+00:00", "Z"),
                        "end_at": end_at.isoformat().replace("+00:00", "Z"),
                    },
                ),
            ):
                result = request_json(config, path, params=params)
                results.append(result)
                print(f"{path} -> {result['status_code']} {result['shape']}", flush=True)

    snapshot = {
        "captured_at": captured_at.isoformat(),
        "base_url": config["base_url"],
        "api_key_used": "<ENPHASE_CLOUD_API_KEY>",
        "access_token_used": "<ENPHASE_CLOUD_ACCESS_TOKEN>",
        "results": results,
    }
    output_dir = Path(__file__).resolve().parent / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"evse_api_v4_{captured_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    latest_path = output_dir / "evse_api_v4_latest.json"
    encoded = json.dumps(snapshot, indent=2, sort_keys=True)
    output_path.write_text(f"{encoded}\n", encoding="utf-8")
    latest_path.write_text(f"{encoded}\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
