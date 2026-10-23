import os
from datetime import datetime, timedelta, timezone
from typing import Any

from cloud_common import cloud_get, compact, env_int, json_shape, redact, write_json_snapshot


def parse_systems(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    systems = payload.get("systems") or []
    result = []
    if isinstance(systems, list):
        for item in systems:
            if isinstance(item, dict) and item.get("system_id"):
                result.append(str(item["system_id"]))
    return result


def probe(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    response = cloud_get(path, params=params)
    result: dict[str, Any] = {
        "path": path,
        "params": params or {},
        "status_code": response.status_code,
        "ok": 200 <= response.status_code < 300,
        "headers": {
            "content_type": response.headers.get("content-type", ""),
            "x_ratelimit_limit": response.headers.get("x-ratelimit-limit", ""),
            "x_ratelimit_remaining": response.headers.get("x-ratelimit-remaining", ""),
            "x_ratelimit_reset": response.headers.get("x-ratelimit-reset", ""),
        },
    }
    try:
        payload = response.json()
        result["shape"] = json_shape(payload)
        result["summary"] = compact(redact(payload))
        result["_payload"] = payload
    except ValueError:
        result["shape"] = "text"
        result["summary"] = response.text[:500]
    return result


def without_raw(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for result in results:
        item = dict(result)
        item.pop("_payload", None)
        cleaned.append(item)
    return cleaned


def main() -> None:
    limit = env_int("ENPHASE_CLOUD_ENDPOINT_LIMIT", 6)
    configured_system_id = os.getenv("ENPHASE_CLOUD_SYSTEM_ID", "").strip()
    results: list[dict[str, Any]] = []

    systems_result = probe("/systems")
    results.append(systems_result)
    print(f"/systems -> {systems_result['status_code']} {systems_result['shape']}", flush=True)

    system_ids = [configured_system_id] if configured_system_id else parse_systems(systems_result.get("_payload"))
    if system_ids and len(results) < limit:
        system_id = system_ids[0]
        for path in (
            f"/systems/{system_id}/summary",
            f"/systems/{system_id}/energy_lifetime",
            f"/systems/{system_id}/consumption_lifetime",
        ):
            if len(results) >= limit:
                break
            result = probe(path)
            results.append(result)
            print(f"{path} -> {result['status_code']} {result['shape']}", flush=True)

        end_at = datetime.now(timezone.utc)
        start_at = end_at - timedelta(hours=24)
        for path, params in (
            (
                f"/systems/{system_id}/energy_lifetime",
                {
                    "start_date": start_at.date().isoformat(),
                    "end_date": end_at.date().isoformat(),
                },
            ),
            (
                f"/systems/{system_id}/consumption_lifetime",
                {
                    "start_date": start_at.date().isoformat(),
                    "end_date": end_at.date().isoformat(),
                },
            ),
        ):
            if len(results) >= limit:
                break
            result = probe(path, params=params)
            results.append(result)
            print(f"{path} date_range -> {result['status_code']} {result['shape']}", flush=True)

    output_path, latest_path = write_json_snapshot(
        "api_v4_explore",
        {
            "api_key_used": "<ENPHASE_CLOUD_API_KEY>",
            "access_token_used": "<ENPHASE_CLOUD_ACCESS_TOKEN>",
            "endpoint_limit": limit,
            "results": without_raw(results),
        },
    )
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
