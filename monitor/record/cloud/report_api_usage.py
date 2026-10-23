import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cloud_common import usage_ledger_path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from record.query_helpers import env_timezone

LOCAL_TZ = ZoneInfo(env_timezone("HISTORY_TIMEZONE", "WEATHER_TIMEZONE"))


def parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = parse_timestamp(str(event.get("timestamp", "")))
        if timestamp is None:
            continue
        event["timestamp_dt"] = timestamp
        events.append(event)
    return events


def summarize(events: list[dict], start: datetime, stop: datetime) -> dict:
    selected = [event for event in events if start <= event["timestamp_dt"] < stop]
    by_status = Counter(str(event.get("status_code", "unknown")) for event in selected)
    by_path = Counter(str(event.get("path", "unknown")) for event in selected)
    return {
        "start": start.astimezone(LOCAL_TZ).isoformat(),
        "stop": stop.astimezone(LOCAL_TZ).isoformat(),
        "count": len(selected),
        "by_status": dict(sorted(by_status.items())),
        "top_paths": [
            {"path": path, "count": count}
            for path, count in by_path.most_common(10)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize local Enphase cloud API usage ledger.")
    parser.add_argument("--ledger", default="", help="Override ledger path. Defaults to cloud_common usage ledger.")
    parser.add_argument("--monthly-limit", type=int, default=int(os.getenv("ENPHASE_CLOUD_MONTHLY_LIMIT", "1000")))
    parser.add_argument("--minute-limit", type=int, default=int(os.getenv("ENPHASE_CLOUD_MINUTE_LIMIT", "10")))
    args = parser.parse_args()

    path = Path(args.ledger) if args.ledger else usage_ledger_path()
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(LOCAL_TZ)
    month_start = datetime(local_now.year, local_now.month, 1, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    next_month = (
        datetime(local_now.year + 1, 1, 1, tzinfo=LOCAL_TZ)
        if local_now.month == 12
        else datetime(local_now.year, local_now.month + 1, 1, tzinfo=LOCAL_TZ)
    ).astimezone(timezone.utc)
    minute_start = now - timedelta(minutes=1)
    last_30_days = now - timedelta(days=30)

    events = load_events(path)
    current_month = summarize(events, month_start, next_month)
    last_30 = summarize(events, last_30_days, now + timedelta(seconds=1))
    last_minute = summarize(events, minute_start, now + timedelta(seconds=1))

    print(
        json.dumps(
            {
                "ledger": str(path),
                "now": local_now.isoformat(),
                "total_recorded_calls": len(events),
                "current_month": {
                    **current_month,
                    "limit": args.monthly_limit,
                    "usage_fraction": round(current_month["count"] / args.monthly_limit, 3)
                    if args.monthly_limit
                    else None,
                },
                "last_30_days": {
                    **last_30,
                    "limit_reference": args.monthly_limit,
                    "usage_fraction": round(last_30["count"] / args.monthly_limit, 3)
                    if args.monthly_limit
                    else None,
                },
                "last_minute": {
                    **last_minute,
                    "limit": args.minute_limit,
                    "usage_fraction": round(last_minute["count"] / args.minute_limit, 3)
                    if args.minute_limit
                    else None,
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
