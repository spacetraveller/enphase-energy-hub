"""Discover Gateway inverters and maintain a private local alias inventory."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.append(str(Path(__file__).resolve().parents[2]))

from monitor import fetch_json
from record.solar_layout import display_path, local_path


def read_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("inverters"), list):
        raise ValueError("Existing private inverter inventory has an unexpected format.")
    by_serial = {}
    for entry in value["inverters"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("serial_number"), str):
            raise ValueError("Existing private inverter inventory has an invalid entry.")
        serial = entry["serial_number"]
        if not serial or serial in by_serial:
            raise ValueError("Existing private inverter inventory has a missing or duplicate serial.")
        by_serial[serial] = entry
    aliases = [entry.get("alias") for entry in by_serial.values() if isinstance(entry.get("alias"), str)]
    if len(aliases) != len(set(aliases)):
        raise ValueError("Existing private inverter inventory has duplicate aliases.")
    return by_serial


def main() -> int:
    parser = argparse.ArgumentParser(description="Save a private inventory of inverters reported by the local Gateway.")
    parser.add_argument("--output", default="inverters.json", help="filename inside the ignored local-solar-layout directory")
    args = parser.parse_args()

    try:
        path = local_path(args.output)
        existing = read_existing(path)
        response = fetch_json("/api/v1/production/inverters", timeout=10)
        if not isinstance(response, list):
            raise ValueError("Gateway inverter response was not a list.")

        discovered = {}
        for item in response:
            if not isinstance(item, dict) or not isinstance(item.get("serialNumber"), (str, int)):
                raise ValueError("Gateway inverter response contained an invalid entry.")
            serial = str(item["serialNumber"]).strip()
            if not serial or serial in discovered:
                raise ValueError("Gateway inverter response contained a missing or duplicate serial.")
            discovered[serial] = item

        now = datetime.now(timezone.utc).isoformat()
        inventory = []
        used_aliases = {
            entry["alias"] for entry in existing.values()
            if isinstance(entry.get("alias"), str) and entry["alias"].strip()
        }
        next_alias_number = 1
        for serial in sorted(set(existing) | set(discovered)):
            previous = existing.get(serial, {})
            item = discovered.get(serial)
            alias = previous.get("alias")
            if not isinstance(alias, str) or not alias.strip():
                while f"INV-{next_alias_number:02d}" in used_aliases:
                    next_alias_number += 1
                alias = f"INV-{next_alias_number:02d}"
                used_aliases.add(alias)
                next_alias_number += 1
            inventory.append({
                "serial_number": serial,
                "alias": alias,
                "dev_type": item.get("devType") if item is not None else previous.get("dev_type"),
                "present": item is not None,
            })

        value = {"version": 1, "discovered_at": now, "inverters": inventory}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except (requests.RequestException, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Inverter discovery failed ({type(exc).__name__}); existing inventory was kept.", file=sys.stderr)
        return 1

    print(f"Discovered {len(discovered)} inverter(s). Private inventory saved to {display_path(path)}.")
    if existing:
        print("Existing aliases were kept; previously known inverters absent from this response are marked present=false.")
    print("Review the file locally to match serials with installer records. Do not share the file or paste its contents into chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
