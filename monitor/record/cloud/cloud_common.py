import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests


DEFAULT_API_BASE = "https://api.enphaseenergy.com/api/v4"
DEFAULT_OAUTH_BASE = "https://api.enphaseenergy.com/oauth"
DEFAULT_REDIRECT_URI = "https://api.enphaseenergy.com/oauth/redirect_uri"
SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "client_secret",
    "authorization",
    "key",
}
_LAST_CLOUD_GET_MONOTONIC: float | None = None


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


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def local_history_timezone() -> ZoneInfo:
    """Use the configured local timezone for cloud history and EV day ranges."""
    for name in ("ENPHASE_HISTORY_TIMEZONE", "HISTORY_TIMEZONE", "WEATHER_TIMEZONE"):
        value = os.getenv(name)
        if value:
            return ZoneInfo(value)
    return ZoneInfo("UTC")


def api_base() -> str:
    return os.getenv("ENPHASE_CLOUD_API_BASE", DEFAULT_API_BASE).rstrip("/")


def oauth_base() -> str:
    return os.getenv("ENPHASE_CLOUD_OAUTH_BASE", DEFAULT_OAUTH_BASE).rstrip("/")


def redirect_uri() -> str:
    return os.getenv("ENPHASE_CLOUD_REDIRECT_URI", DEFAULT_REDIRECT_URI)


def auth_url() -> str:
    provided = os.getenv("ENPHASE_CLOUD_AUTHORIZATION_URL", "").strip()
    if provided:
        return provided
    client_id = env_required("ENPHASE_CLOUD_CLIENT_ID")
    return f"{oauth_base()}/authorize?{urlencode({'response_type': 'code', 'client_id': client_id})}"


def authorization_header() -> str:
    raw = f"{env_required('ENPHASE_CLOUD_CLIENT_ID')}:{env_required('ENPHASE_CLOUD_CLIENT_SECRET')}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def with_query(url: str, values: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: value for key, value in values.items() if value})
    return urlunparse(parsed._replace(query=urlencode(query)))


def token_request(params: dict[str, str]) -> dict[str, Any]:
    response = requests.post(
        f"{oauth_base()}/token",
        params=params,
        headers={"Authorization": authorization_header(), "Accept": "application/json"},
        timeout=30,
    )
    payload = response.json()
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"token_request_failed status={response.status_code} body={redact(payload)}")
    return payload


def usage_dir() -> Path:
    return Path(os.getenv("ENPHASE_CLOUD_USAGE_DIR", Path(__file__).resolve().parent / "usage"))


def usage_ledger_path() -> Path:
    return Path(os.getenv("ENPHASE_CLOUD_USAGE_LEDGER", usage_dir() / "api_calls.jsonl"))


def record_cloud_usage(
    *,
    method: str,
    path: str,
    status_code: int,
    elapsed_ms: int,
    attempt: int,
    rate_headers: dict[str, str],
) -> None:
    if os.getenv("ENPHASE_CLOUD_USAGE_LEDGER_ENABLED", "true").lower() in {"0", "false", "no"}:
        return
    output_path = usage_ledger_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "path": path,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
        "attempt": attempt,
        "rate_headers": rate_headers,
    }
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def cloud_get(path: str, params: dict[str, str] | None = None) -> requests.Response:
    global _LAST_CLOUD_GET_MONOTONIC

    min_interval_seconds = max(
        0.0,
        env_float(
            "ENPHASE_CLOUD_MIN_REQUEST_INTERVAL_SECONDS",
            env_float("ENPHASE_CLOUD_REQUEST_DELAY_SECONDS", 8.0),
        ),
    )
    max_retries = max(0, env_int("ENPHASE_CLOUD_RATE_LIMIT_RETRIES", 2))
    url = f"{api_base()}{path}"
    headers = {
        "Authorization": f"Bearer {env_required('ENPHASE_CLOUD_ACCESS_TOKEN')}",
        "Accept": "application/json",
        "key": env_required("ENPHASE_CLOUD_API_KEY"),
    }

    for attempt in range(max_retries + 1):
        if _LAST_CLOUD_GET_MONOTONIC is not None and min_interval_seconds > 0:
            elapsed = time.monotonic() - _LAST_CLOUD_GET_MONOTONIC
            if elapsed < min_interval_seconds:
                time.sleep(min_interval_seconds - elapsed)

        started = time.monotonic()
        response = requests.get(url, params=params, headers=headers, timeout=30)
        _LAST_CLOUD_GET_MONOTONIC = time.monotonic()
        record_cloud_usage(
            method="GET",
            path=path,
            status_code=response.status_code,
            elapsed_ms=int((_LAST_CLOUD_GET_MONOTONIC - started) * 1000),
            attempt=attempt + 1,
            rate_headers={
                key: value
                for key, value in response.headers.items()
                if key.lower().startswith("x-ratelimit") or key.lower() == "retry-after"
            },
        )

        if response.status_code != 429 or attempt >= max_retries:
            return response

        retry_after = response.headers.get("Retry-After")
        reset_after = response.headers.get("X-RateLimit-Reset")
        wait_seconds = min_interval_seconds
        for raw in (retry_after, reset_after):
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            # Some APIs use epoch seconds for X-RateLimit-Reset, while others use
            # seconds-until-reset. Treat large values as epoch timestamps.
            if value > 1_000_000_000:
                value = max(0.0, value - time.time())
            wait_seconds = max(wait_seconds, value)
        time.sleep(min(wait_seconds, 120.0))

    return response


def require_cloud_success(response: requests.Response, operation: str) -> None:
    if 200 <= response.status_code < 300:
        return
    if response.status_code == 401:
        raise RuntimeError(
            f"{operation}_failed status=401 authentication_required=true "
            "action=refresh_oauth_token_and_retry"
        )
    raise RuntimeError(f"{operation}_failed status={response.status_code}")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in SECRET_KEYS or "token" in lowered or "secret" in lowered:
                result[key] = "<redacted>"
            else:
                result[key] = redact(child)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def compact(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {key: compact(value[key], depth + 1) for key in list(value)[:16]}
    if isinstance(value, list):
        return [compact(item, depth + 1) for item in value[:5]]
    return value


def json_shape(value: Any) -> str:
    if isinstance(value, dict):
        keys = list(value.keys())
        return f"object[{len(keys)}] keys={keys[:16]}"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    return type(value).__name__


def write_json_snapshot(prefix: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    captured_at = datetime.now(timezone.utc)
    snapshot = {"captured_at": captured_at.isoformat(), **payload}
    output_dir = Path(__file__).resolve().parent / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{prefix}_{captured_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    latest_path = output_dir / f"{prefix}_latest.json"
    encoded = json.dumps(snapshot, indent=2, sort_keys=True)
    output_path.write_text(f"{encoded}\n", encoding="utf-8")
    latest_path.write_text(f"{encoded}\n", encoding="utf-8")
    return output_path, latest_path


def write_token_env(path: str, payload: dict[str, Any]) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "ENPHASE_CLOUD_ACCESS_TOKEN": payload.get("access_token", ""),
        "ENPHASE_CLOUD_REFRESH_TOKEN": payload.get("refresh_token", ""),
        "ENPHASE_CLOUD_TOKEN_EXPIRES_IN": payload.get("expires_in", ""),
        "ENPHASE_CLOUD_TOKEN_TYPE": payload.get("token_type", ""),
    }
    lines = output_path.read_text(encoding="utf-8").splitlines() if output_path.exists() else []
    updated = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0]
        if key in values:
            lines[index] = f"{key}={values[key]}"
            updated.add(key)
    if lines and updated != values.keys():
        lines.append("")
    for key, value in values.items():
        if key not in updated:
            lines.append(f"{key}={value}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path
