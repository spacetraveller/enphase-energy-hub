import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SOURCE_MEASUREMENT = "enphase_energy_flow"
V2_MEASUREMENT = "enphase_energy_flow_v2"
TARGET_MEASUREMENT = "enphase_energy_flow"
INTEGER_FIELDS = {
    "active_count",
    "reading_time",
    "wh_today",
}
DEFAULT_ENGINE_PATH = "/var/lib/influxdb2/engine"
DEFAULT_INFLUXDB_URL = "http://localhost:8086"


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def env_config() -> dict[str, str]:
    config = load_dotenv(repo_root() / ".env")
    merged = {**config, **os.environ}
    required = ["INFLUXDB_TOKEN", "INFLUXDB_ORG", "INFLUXDB_BUCKET"]
    missing = [name for name in required if not merged.get(name)]
    if missing:
        raise RuntimeError(f"Missing required environment values: {', '.join(missing)}")
    return merged


def influx_request(
    url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    params: dict[str, str] | None = None,
    body: bytes | None = None,
    content_type: str = "application/json",
) -> bytes:
    full_url = f"{url.rstrip('/')}{path}"
    if params:
        full_url = f"{full_url}?{urlencode(params)}"
    request = Request(full_url, data=body, method=method)
    request.add_header("Authorization", f"Token {token}")
    if body is not None:
        request.add_header("Content-Type", content_type)
    with urlopen(request, timeout=60) as response:
        return response.read()


def bucket_id(config: dict[str, str], influx_url: str) -> str:
    raw = influx_request(
        influx_url,
        config["INFLUXDB_TOKEN"],
        "/api/v2/buckets",
        params={"name": config["INFLUXDB_BUCKET"]},
    )
    payload = json.loads(raw.decode("utf-8"))
    buckets = payload.get("buckets", [])
    if not buckets:
        raise RuntimeError(f'Bucket not found: {config["INFLUXDB_BUCKET"]}')
    return str(buckets[0]["id"])


def run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout


def export_measurement(
    bucket: str,
    measurement: str,
    *,
    start: str,
    end: str,
    engine_path: str,
) -> list[str]:
    output = run(
        [
            "docker",
            "exec",
            "enphase-influxdb",
            "influxd",
            "inspect",
            "export-lp",
            "--bucket-id",
            bucket,
            "--engine-path",
            engine_path,
            "--measurement",
            measurement,
            "--start",
            start,
            "--end",
            end,
            "--output-path",
            "-",
        ]
    )
    prefixes = (f"{measurement} ", f"{measurement},")
    return [line for line in output.splitlines() if line.startswith(prefixes)]


def read_export(path: Path, measurements: set[str]) -> list[str]:
    lines = []
    prefixes = tuple(
        prefix
        for measurement in measurements
        for prefix in (f"{measurement} ", f"{measurement},")
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefixes):
            lines.append(line)
    return lines


def split_unescaped_once(value: str, separator: str) -> tuple[str, str]:
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == separator:
            return value[:index], value[index + 1 :]
    return value, ""


def split_unescaped(value: str, separator: str) -> list[str]:
    parts = []
    remaining = value
    while remaining:
        part, remaining = split_unescaped_once(remaining, separator)
        parts.append(part)
    return parts


def parse_line(line: str) -> tuple[str, str, str]:
    key_part, rest = split_unescaped_once(line, " ")
    field_part, timestamp = split_unescaped_once(rest, " ")
    if not key_part or not field_part or not timestamp:
        raise ValueError(f"Unsupported line protocol line: {line}")
    return key_part, field_part, timestamp


def field_type(raw_value: str) -> str:
    lowered = raw_value.lower()
    if raw_value.startswith('"'):
        return "string"
    if lowered in {"true", "false", "t", "f"}:
        return "bool"
    if raw_value.endswith("i"):
        return "int"
    return "float"


def numeric_value(raw_value: str) -> float:
    if raw_value.endswith("i"):
        return float(raw_value[:-1])
    return float(raw_value)


def format_number(field: str, raw_value: str) -> tuple[str, str]:
    value = numeric_value(raw_value)
    if field in INTEGER_FIELDS:
        return f"{int(value)}i", "int"
    return repr(float(value)), "float"


def rewrite_key(key_part: str, target: str) -> str:
    measurement, tags = split_unescaped_once(key_part, ",")
    if tags:
        return f"{target},{tags}"
    return target


def rewrite_line(
    line: str,
    *,
    target: str,
    summary: dict[str, Counter[str]],
) -> str:
    key_part, field_part, timestamp = parse_line(line)
    rewritten_fields = []
    for field_pair in split_unescaped(field_part, ","):
        field, raw_value = split_unescaped_once(field_pair, "=")
        original_type = field_type(raw_value)
        if original_type in {"int", "float"}:
            repaired_value, repaired_type = format_number(field, raw_value)
        else:
            repaired_value, repaired_type = raw_value, original_type
        summary[field][f"{original_type}->{repaired_type}"] += 1
        rewritten_fields.append(f"{field}={repaired_value}")
    return f"{rewrite_key(key_part, target)} {','.join(rewritten_fields)} {timestamp}"


def timestamp_range(lines: list[str]) -> tuple[str, str] | None:
    timestamps = []
    for line in lines:
        try:
            _, _, timestamp = parse_line(line)
            timestamps.append(int(timestamp))
        except ValueError:
            continue
    if not timestamps:
        return None

    def render(ns: int) -> str:
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()

    return render(min(timestamps)), render(max(timestamps))


def delete_measurement(config: dict[str, str], influx_url: str, measurement: str) -> None:
    body = json.dumps(
        {
            "start": "1970-01-01T00:00:00Z",
            "stop": "2100-01-01T00:00:00Z",
            "predicate": f'_measurement="{measurement}"',
        }
    ).encode("utf-8")
    influx_request(
        influx_url,
        config["INFLUXDB_TOKEN"],
        "/api/v2/delete",
        method="POST",
        params={"org": config["INFLUXDB_ORG"], "bucket": config["INFLUXDB_BUCKET"]},
        body=body,
    )


def write_lines(config: dict[str, str], influx_url: str, lines: list[str], chunk_size: int) -> None:
    params = {
        "org": config["INFLUXDB_ORG"],
        "bucket": config["INFLUXDB_BUCKET"],
        "precision": "ns",
    }
    for offset in range(0, len(lines), chunk_size):
        chunk = "\n".join(lines[offset : offset + chunk_size]).encode("utf-8")
        influx_request(
            influx_url,
            config["INFLUXDB_TOKEN"],
            "/api/v2/write",
            method="POST",
            params=params,
            body=chunk,
            content_type="text/plain; charset=utf-8",
        )


def stop_monitor() -> None:
    run(["docker", "compose", "stop", "monitor"])


def start_monitor() -> None:
    run(["docker", "compose", "up", "-d", "monitor"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export, normalize, and optionally rebuild the Enphase Energy Flow measurement."
    )
    parser.add_argument("--start", default="2026-05-03T00:00:00Z")
    parser.add_argument("--end", default="2100-01-01T00:00:00Z")
    parser.add_argument(
        "--influx-url",
        default=os.getenv("REPAIR_INFLUXDB_URL")
        or os.getenv("INFLUXDB_URL")
        or DEFAULT_INFLUXDB_URL,
    )
    parser.add_argument("--engine-path", default=DEFAULT_ENGINE_PATH)
    parser.add_argument("--target-measurement", default=TARGET_MEASUREMENT)
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Read exported line protocol from this file instead of running docker/influxd export.",
    )
    parser.add_argument("--output", default="")
    parser.add_argument("--include-v2", action="store_true", help="Also merge points from enphase_energy_flow_v2.")
    parser.add_argument("--execute", action="store_true", help="Delete/rebuild the target measurement.")
    parser.add_argument("--stop-monitor", action="store_true", help="Stop monitor before executing repair.")
    parser.add_argument("--restart-monitor", action="store_true", help="Start monitor after executing repair.")
    parser.add_argument("--delete-v2", action="store_true", help="Delete enphase_energy_flow_v2 after successful import.")
    parser.add_argument("--chunk-size", type=int, default=5000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = env_config()
    root = repo_root()
    output_path = (
        Path(args.output)
        if args.output
        else root / "monitor" / "record" / "debug" / "exports" / "energy_flow_repaired.lp"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sources = [SOURCE_MEASUREMENT]
    if args.include_v2:
        sources.append(V2_MEASUREMENT)

    exported: list[str] = []
    if args.input:
        measurements = set(sources)
        for input_path in args.input:
            lines = read_export(Path(input_path), measurements)
            print(f"read {input_path}: {len(lines)} source lines")
            exported.extend(lines)
    else:
        bucket = bucket_id(config, args.influx_url)
        for source in sources:
            lines = export_measurement(
                bucket,
                source,
                start=args.start,
                end=args.end,
                engine_path=args.engine_path,
            )
            print(f"exported {source}: {len(lines)} lines")
            exported.extend(lines)

    summary: dict[str, Counter[str]] = defaultdict(Counter)
    repaired = [
        rewrite_line(line, target=args.target_measurement, summary=summary)
        for line in exported
    ]
    output_path.write_text("\n".join(repaired) + ("\n" if repaired else ""), encoding="utf-8")

    print(f"wrote_cleaned_line_protocol={output_path}")
    print(f"cleaned_lines={len(repaired)}")
    range_info = timestamp_range(repaired)
    if range_info:
        print(f"timestamp_range={range_info[0]}..{range_info[1]}")
    print("field_type_rewrites:")
    for field in sorted(summary):
        print(f"  {field}: {dict(summary[field])}")

    if not args.execute:
        print("dry_run=true")
        print("No InfluxDB data was deleted or imported.")
        return

    if args.stop_monitor:
        print("stopping monitor")
        stop_monitor()

    print(f"deleting measurement {args.target_measurement}")
    delete_measurement(config, args.influx_url, args.target_measurement)
    print(f"importing {len(repaired)} cleaned lines")
    write_lines(config, args.influx_url, repaired, args.chunk_size)

    if args.delete_v2:
        print(f"deleting measurement {V2_MEASUREMENT}")
        delete_measurement(config, args.influx_url, V2_MEASUREMENT)

    if args.restart_monitor:
        print("starting monitor")
        start_monitor()

    print("repair_complete=true")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"repair_failed={exc.__class__.__name__}: {exc}", file=sys.stderr)
        raise
