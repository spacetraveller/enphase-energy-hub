import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def build_url() -> str:
    host = env_required("ENPHASE_HOST").strip("/")
    scheme = os.getenv("ENPHASE_INFO_SCHEME", "http").strip(":/")
    path = os.getenv("ENPHASE_INFO_PATH", "/info")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{scheme}://{host}{path}"


def child_text(element: ElementTree.Element | None, name: str) -> str:
    if element is None:
        return ""
    child = element.find(name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def parse_info(xml_content: bytes) -> dict[str, Any]:
    root = ElementTree.fromstring(xml_content)
    device = root.find("device")
    build_info = root.find("build_info")

    return {
        "envoy_time": child_text(root, "time"),
        "device": {
            "serial_number": child_text(device, "sn"),
            "part_number": child_text(device, "pn"),
            "software": child_text(device, "software"),
            "euaid": child_text(device, "euaid"),
            "seqnum": child_text(device, "seqnum"),
            "api_version": child_text(device, "apiver"),
            "integrated_meter": child_text(device, "imeter"),
        },
        "web_tokens": child_text(root, "web-tokens"),
        "packages": [
            {
                "name": package.attrib.get("name", ""),
                "part_number": child_text(package, "pn"),
                "version": child_text(package, "version"),
                "build": child_text(package, "build"),
            }
            for package in root.findall("package")
        ],
        "build_info": {
            "build_id": child_text(build_info, "build_id"),
            "build_time_gmt": child_text(build_info, "build_time_gmt"),
            "release_stage": child_text(build_info, "release_stage"),
            "release_version": child_text(build_info, "release_ver"),
        },
    }


def main() -> None:
    url = build_url()
    timeout = env_int("PROBE_TIMEOUT_SECONDS", 5)
    captured_at = datetime.now(timezone.utc)

    response = requests.get(url, timeout=timeout, verify=False)
    response.raise_for_status()

    snapshot = {
        "captured_at": captured_at.isoformat(),
        "source_url": url,
        "status_code": response.status_code,
        "info": parse_info(response.content),
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
