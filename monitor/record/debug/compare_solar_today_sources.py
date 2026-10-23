import os
import sys
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor import fetch_json  # noqa: E402


def find_production_item(payload: dict[str, Any]) -> dict[str, Any] | None:
    for item in payload.get("production", []):
        if item.get("measurementType") == "production" or item.get("type") == "eim":
            return item
    return None


def main() -> None:
    timeout = int(os.getenv("PRODUCTION_TIMEOUT_SECONDS", "15"))
    energy_path = os.getenv("ENERGY_FLOW_PATH", "/production.json")
    totals_path = os.getenv("PRODUCTION_TOTALS_PATH", "/api/v1/production")

    for label, path in (("energy_flow", energy_path), ("totals", totals_path), ("api_v1", "/api/v1/production")):
        try:
            payload = fetch_json(path, timeout)
        except Exception as exc:
            print(f"{label} path={path} error={exc.__class__.__name__}")
            continue

        if isinstance(payload, dict) and "production" in payload:
            item = find_production_item(payload)
            print(
                "{label} path={path} type=production_json wNow={w_now} whToday={wh_today} whLifetime={wh_lifetime} readingTime={reading_time}".format(
                    label=label,
                    path=path,
                    w_now=item.get("wNow") if item else None,
                    wh_today=item.get("whToday") if item else None,
                    wh_lifetime=item.get("whLifetime") if item else None,
                    reading_time=item.get("readingTime") if item else None,
                )
            )
        elif isinstance(payload, dict):
            print(
                "{label} path={path} type=api_v1 wattsNow={watts_now} wattHoursToday={wh_today} wattHoursLifetime={wh_lifetime}".format(
                    label=label,
                    path=path,
                    watts_now=payload.get("wattsNow"),
                    wh_today=payload.get("wattHoursToday"),
                    wh_lifetime=payload.get("wattHoursLifetime"),
                )
            )
        else:
            print(f"{label} path={path} type={type(payload).__name__}")


if __name__ == "__main__":
    main()
