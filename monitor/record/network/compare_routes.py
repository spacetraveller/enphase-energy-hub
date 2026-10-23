import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def headers() -> dict[str, str]:
    token = env_required("ENPHASE_ACCESS_TOKEN")
    request_host = os.getenv("ENPHASE_REQUEST_HOST", "").strip("/")
    result = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Connection": "close",
    }
    if request_host:
        result["Host"] = request_host
    return result


def probe(url: str, timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = requests.get(
            url,
            headers=headers(),
            timeout=timeout,
            verify=False,
            allow_redirects=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "ok": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "response_ms": round(elapsed_ms, 1),
            "error": "",
        }
    except requests.RequestException as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "ok": False,
            "status_code": None,
            "response_ms": round(elapsed_ms, 1),
            "error": exc.__class__.__name__,
        }


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    successes = [sample for sample in samples if sample["ok"]]
    latencies = [sample["response_ms"] for sample in successes]
    errors: dict[str, int] = {}
    for sample in samples:
        if sample["ok"]:
            continue
        key = sample["error"] or f"HTTP {sample['status_code']}"
        errors[key] = errors.get(key, 0) + 1

    summary: dict[str, Any] = {
        "attempts": len(samples),
        "successes": len(successes),
        "success_rate": round(len(successes) / len(samples), 4) if samples else 0,
        "errors": errors,
    }
    if latencies:
        summary.update(
            {
                "min_ms": min(latencies),
                "max_ms": max(latencies),
                "avg_ms": round(sum(latencies) / len(latencies), 1),
            }
        )
    return summary


def main() -> None:
    path = os.getenv("ENPHASE_ROUTE_TEST_PATH", "/auth/check_jwt")
    if not path.startswith("/"):
        path = f"/{path}"

    hostname = env_required("ENPHASE_HOST").strip("/")
    direct_host = env_required("ENPHASE_API_HOST").strip("/")
    scheme = os.getenv("ENPHASE_API_SCHEME", "https").strip(":/")
    count = env_int("ROUTE_TEST_COUNT", 20)
    delay_seconds = env_int("ROUTE_TEST_DELAY_SECONDS", 5)
    timeout = env_int("ROUTE_TEST_TIMEOUT_SECONDS", env_int("PRODUCTION_TIMEOUT_SECONDS", 15))
    captured_at = datetime.now(timezone.utc)

    routes = [
        {"name": "hostname", "url": f"{scheme}://{hostname}{path}"},
        {"name": "direct_ip", "url": f"{scheme}://{direct_host}{path}"},
    ]
    results: dict[str, Any] = {}

    for attempt in range(1, count + 1):
        print(f"attempt {attempt}/{count}", flush=True)
        for route in routes:
            sample = probe(route["url"], timeout)
            results.setdefault(route["name"], {"url": route["url"], "samples": []})
            results[route["name"]]["samples"].append(sample)
            status = sample["status_code"] if sample["status_code"] is not None else sample["error"]
            print(f"  {route['name']} -> {status} {sample['response_ms']}ms", flush=True)
        if attempt < count:
            time.sleep(delay_seconds)

    for route_result in results.values():
        route_result["summary"] = summarize(route_result["samples"])

    snapshot = {
        "captured_at": captured_at.isoformat(),
        "path": path,
        "count": count,
        "delay_seconds": delay_seconds,
        "timeout_seconds": timeout,
        "request_host": os.getenv("ENPHASE_REQUEST_HOST", "").strip("/"),
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
