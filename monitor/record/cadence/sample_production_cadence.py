import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def production_url() -> str:
    host = os.environ["ENPHASE_API_HOST"].strip("/")
    scheme = os.getenv("ENPHASE_API_SCHEME", "https").strip(":/")
    path = os.getenv("ENERGY_FLOW_PATH", "/production.json")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{scheme}://{host}{path}"


def headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {os.environ['ENPHASE_ACCESS_TOKEN']}",
        "Connection": "close",
        "Host": os.getenv("ENPHASE_REQUEST_HOST", "envoy.local").strip("/"),
    }


def compact(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section in ("production", "consumption", "storage"):
        for item in data.get(section, []):
            key = f"{section}:{item.get('measurementType') or item.get('type')}"
            result[key] = {
                "wNow": item.get("wNow"),
                "whToday": item.get("whToday"),
                "readingTime": item.get("readingTime"),
                "activeCount": item.get("activeCount"),
            }
    return result


def changed_fields(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    if previous is None:
        return list(current.keys())

    changes = []
    for key, values in current.items():
        if previous.get(key) != values:
            changes.append(key)
    return changes


def main() -> None:
    interval = max(5, env_int("CADENCE_TEST_INTERVAL_SECONDS", 5))
    samples = env_int("CADENCE_TEST_SAMPLES", 36)
    timeout = env_int("CADENCE_TEST_TIMEOUT_SECONDS", 15)
    url = production_url()
    started_at = datetime.now(timezone.utc)
    previous: dict[str, Any] | None = None
    results = []

    print(f"Sampling {url} every {interval}s for {samples} samples", flush=True)

    for sample_number in range(1, samples + 1):
        sample_started = time.perf_counter()
        captured_at = datetime.now(timezone.utc)
        try:
            response = requests.get(
                url,
                headers=headers(),
                timeout=timeout,
                verify=False,
                allow_redirects=False,
            )
            response.raise_for_status()
            current = compact(response.json())
            changes = changed_fields(previous, current)
            previous = current
            result = {
                "sample": sample_number,
                "captured_at": captured_at.isoformat(),
                "ok": True,
                "status_code": response.status_code,
                "response_ms": round((time.perf_counter() - sample_started) * 1000, 1),
                "changed_groups": changes,
                "values": current,
            }
            print(
                f"{sample_number:02d}: ok changes={len(changes)} response_ms={result['response_ms']}",
                flush=True,
            )
        except requests.RequestException as exc:
            result = {
                "sample": sample_number,
                "captured_at": captured_at.isoformat(),
                "ok": False,
                "error": exc.__class__.__name__,
                "response_ms": round((time.perf_counter() - sample_started) * 1000, 1),
            }
            print(f"{sample_number:02d}: {result['error']} response_ms={result['response_ms']}", flush=True)

        results.append(result)
        if sample_number < samples:
            time.sleep(interval)

    output_dir = Path(__file__).resolve().parent / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    latest_path = output_dir / "latest.json"
    snapshot = {
        "started_at": started_at.isoformat(),
        "interval_seconds": interval,
        "samples": samples,
        "url": url,
        "results": results,
    }
    encoded = json.dumps(snapshot, indent=2, sort_keys=True)
    output_path.write_text(f"{encoded}\n", encoding="utf-8")
    latest_path.write_text(f"{encoded}\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()
