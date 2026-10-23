import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_ENDPOINTS = [
    "/ivp/pdm/device_data",
    "/ivp/pdm/chargers",
    "/ivp/pdm/charger",
    "/ivp/pdm/ev_chargers",
    "/ivp/pdm/ev_charger",
    "/ivp/pdm/evse",
    "/ivp/pdm/connectors",
    "/ivp/peb/managed_loads",
    "/ivp/peb/ev_chargers",
    "/ivp/peb/ev_charger",
    "/ivp/peb/evse",
    "/ivp/peb/energy_router",
    "/ivp/ocpp",
    "/ivp/ocpp/status",
]

SERIAL_ENDPOINTS = [
    "/ivp/pdm/charger/{serial}/status",
    "/ivp/pdm/charger/{serial}/summary",
    "/ivp/peb/charger/{serial}/schedule",
    "/ivp/peb/ev_charger/{serial}/status",
    "/ivp/peb/ev_charger/{serial}/metrics",
    "/ivp/peb/ev_charger/{serial}/power",
    "/ivp/peb/ev_charger/{serial}/sessions",
    "/ivp/peb/ev_charger/{serial}/summary",
]

KEYWORDS = ("ev", "evse", "charger", "charge", "ocpp", "connector", "managed")


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def json_shape(value: Any) -> str:
    if isinstance(value, dict):
        keys = list(value.keys())
        return f"object[{len(keys)}] keys: {', '.join(str(key) for key in keys[:12])}"
    if isinstance(value, list):
        if not value:
            return "array[0]"
        first = value[0]
        if isinstance(first, dict):
            return f"array[{len(value)}] first keys: {', '.join(first.keys())}"
        return f"array[{len(value)}] first={type(first).__name__}"
    return type(value).__name__


def find_keyword_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if any(keyword in str(key).lower() for keyword in KEYWORDS):
                paths.append(path)
            paths.extend(find_keyword_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value[:5]):
            paths.extend(find_keyword_paths(child, f"{prefix}[{index}]"))
    elif isinstance(value, str) and any(keyword in value.lower() for keyword in KEYWORDS):
        paths.append(prefix)
    return paths


def summarize_text(text: str) -> tuple[str, list[str]]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        normalized = " ".join(text.split())
        hits = [keyword for keyword in KEYWORDS if keyword in normalized.lower()]
        return normalized[:300], hits

    return json_shape(parsed), find_keyword_paths(parsed)[:30]


def probe(headers: dict[str, str], base_url: str, endpoint: str, timeout: int) -> dict[str, Any]:
    url = f"{base_url}{endpoint}"
    result: dict[str, Any] = {
        "endpoint": endpoint,
        "status_code": None,
        "content_type": "",
        "summary": "",
        "keyword_paths": [],
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
        result["summary"], result["keyword_paths"] = summarize_text(response.text)
    except requests.RequestException as exc:
        result["error"] = exc.__class__.__name__
    return result


def main() -> None:
    host = env_required("ENPHASE_HOST").strip("/")
    scheme = os.getenv("ENPHASE_API_SCHEME", "https").strip(":/")
    request_host = os.getenv("ENPHASE_REQUEST_HOST", "").strip("/")
    timeout = int(os.getenv("PROBE_TIMEOUT_SECONDS", "10"))
    serials = [
        serial.strip()
        for serial in os.getenv("EV_CHARGER_SERIALS", "").split(",")
        if serial.strip()
    ]
    endpoints = list(BASE_ENDPOINTS)
    for serial in serials:
        endpoints.extend(endpoint.format(serial=serial) for endpoint in SERIAL_ENDPOINTS)

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {env_required('ENPHASE_ACCESS_TOKEN')}",
        "Connection": "close",
    }
    if request_host:
        headers["Host"] = request_host

    base_url = f"{scheme}://{host}"
    captured_at = datetime.now(timezone.utc)
    results = []
    for endpoint in endpoints:
        result = probe(headers, base_url, endpoint, timeout)
        results.append(result)
        status = result["status_code"] if result["status_code"] is not None else result["error"]
        print(f"{endpoint} -> {status} {result['summary']}", flush=True)
        if result["keyword_paths"]:
            print(f"  keyword_paths={result['keyword_paths'][:8]}", flush=True)

    snapshot = {
        "captured_at": captured_at.isoformat(),
        "host": host,
        "request_host": request_host,
        "auth_header_used": "Bearer <ENPHASE_ACCESS_TOKEN>",
        "serials_tested": serials,
        "results": results,
    }
    output_dir = Path(__file__).resolve().parent / "snapshots"
    output_path = output_dir / f"ev_charger_{captured_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    latest_path = output_dir / "ev_charger_latest.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(snapshot, indent=2, sort_keys=True)
    output_path.write_text(f"{encoded}\n", encoding="utf-8")
    latest_path.write_text(f"{encoded}\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
