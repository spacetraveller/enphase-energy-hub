import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


ENDPOINTS = [
    "/auth/check_jwt",
    "/home",
    "/home.json",
    "/info",
    "/production",
    "/production.json",
    "/production.json?details=1",
    "/api/v1/production",
    "/api/v1/production/inverters",
    "/inventory.json",
    "/ivp/meters",
    "/ivp/meters/readings",
    "/ivp/pdm/device_data",
    "/ivp/ensemble/inventory",
    "/ivp/ensemble/status",
    "/ivp/ensemble/secctrl",
    "/ivp/ensemble/power",
    "/ivp/ensemble/relay",
    "/ivp/ensemble/dry_contacts",
    "/ivp/ss/dry_contact_settings",
    "/ivp/ss/gen_config",
    "/ivp/ss/gen_mode",
    "/ivp/ss/gen_schedule",
    "/uvp/ss/pel_settings",
    "/admin/lib/tariff",
]


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def content_preview(content: str, limit: int = 500) -> str:
    normalized = " ".join(content.split())
    return normalized[:limit]


def json_shape(content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return ""

    if isinstance(parsed, list):
        if not parsed:
            return "array[0]"
        first = parsed[0]
        if isinstance(first, dict):
            return f"array[{len(parsed)}] first keys: {', '.join(first.keys())}"
        return f"array[{len(parsed)}]"

    if isinstance(parsed, dict):
        return f"object keys: {', '.join(parsed.keys())}"

    return type(parsed).__name__


def probe(headers: dict[str, str], url: str, timeout: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "url": url,
        "accessible": False,
        "status_code": None,
        "content_type": "",
        "json_shape": "",
        "preview": "",
        "error": "",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            verify=False,
            allow_redirects=False,
        )
        result["status_code"] = response.status_code
        result["content_type"] = response.headers.get("content-type", "")
        result["accessible"] = 200 <= response.status_code < 300
        result["json_shape"] = json_shape(response.text)
        result["preview"] = content_preview(response.text)
    except requests.RequestException as exc:
        result["error"] = exc.__class__.__name__

    return result


def main() -> None:
    host = env_required("ENPHASE_HOST").strip("/")
    request_host = os.getenv("ENPHASE_REQUEST_HOST", "").strip("/")
    token = env_required("ENPHASE_ACCESS_TOKEN")
    timeout = int(os.getenv("PROBE_TIMEOUT_SECONDS", "10"))
    captured_at = datetime.now(timezone.utc)

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Connection": "close",
    }
    if request_host:
        headers["Host"] = request_host

    results = []
    for endpoint in ENDPOINTS:
        for scheme in ("https", "http"):
            url = f"{scheme}://{host}{endpoint}"
            result = {
                "endpoint": endpoint,
                "scheme": scheme,
                **probe(headers, url, timeout),
            }
            results.append(result)
            status = result["status_code"] if result["status_code"] is not None else result["error"]
            print(f"{scheme} {endpoint} -> {status}", flush=True)

    snapshot = {
        "captured_at": captured_at.isoformat(),
        "host": host,
        "request_host": request_host,
        "auth_header_used": "Bearer <ENPHASE_ACCESS_TOKEN>",
        "results": results,
    }

    output_dir = Path(__file__).resolve().parent / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{captured_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    latest_path = output_dir / "latest.json"

    encoded = json.dumps(snapshot, indent=2, sort_keys=True)
    output_path.write_text(f"{encoded}\n", encoding="utf-8")
    latest_path.write_text(f"{encoded}\n", encoding="utf-8")

    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
