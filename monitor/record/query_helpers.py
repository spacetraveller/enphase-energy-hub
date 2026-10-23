from __future__ import annotations

import os
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient


DEFAULT_TIMEZONE = "UTC"
ENERGY_FLOW_MEASUREMENT = "enphase_energy_flow"
HISTORY_MEASUREMENT = "enphase_history_15m"
HISTORY_PRODUCTION_FIELD = "energy_produced_wh"
METER_READING_MEASUREMENT = "enphase_meter_reading"
SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES


@dataclass(frozen=True)
class DeltaResult:
    kwh: float | None
    first_time: datetime | None
    first_value: float | None
    last_time: datetime | None
    last_value: float | None


@dataclass(frozen=True)
class TimeValueReading:
    time: datetime
    value: float


@dataclass(frozen=True)
class SameTimeComparison:
    target: TimeValueReading
    matches: list[TimeValueReading]


def env_timezone(*names: str, default: str = DEFAULT_TIMEZONE) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def influx_client() -> InfluxDBClient:
    return InfluxDBClient(
        url=os.environ["INFLUXDB_URL"],
        token=os.environ["INFLUXDB_TOKEN"],
        org=os.environ["INFLUXDB_ORG"],
    )


def influx_bucket() -> str:
    return os.environ["INFLUXDB_BUCKET"]


def influx_org() -> str:
    return os.environ["INFLUXDB_ORG"]


def meter_eid_for_role(role: str) -> str:
    """Return a configured or data-discovered Enphase meter EID.

    The meter EIDs are installation-specific. Most systems have a production
    meter with delivered energy and a net meter with both delivered and received
    energy. Users can override discovery with METER_PRODUCTION_EID or
    METER_NET_EID when their meter layout differs.
    """
    env_name = {"production": "METER_PRODUCTION_EID", "net": "METER_NET_EID"}.get(role)
    if env_name is None:
        raise ValueError("role must be 'production' or 'net'")
    configured = os.getenv(env_name, "").strip()
    if configured:
        return configured

    rows = latest_measurement_rows(
        METER_READING_MEASUREMENT,
        start="-30d",
        fields=("act_energy_dlvd", "act_energy_rcvd"),
        group_columns=("eid", "_field"),
        keep_columns=("eid", "_field"),
    )
    fields_by_eid: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        eid = str(row.get("eid") or "").strip()
        field = str(row.get("_field") or "")
        if eid and field:
            fields_by_eid[eid].add(field)

    if role == "production":
        matches = [eid for eid, fields in fields_by_eid.items()
                   if "act_energy_dlvd" in fields and "act_energy_rcvd" not in fields]
    else:
        matches = [eid for eid, fields in fields_by_eid.items()
                   if "act_energy_dlvd" in fields and "act_energy_rcvd" in fields]
    if len(matches) != 1:
        raise RuntimeError(
            f"Could not uniquely discover the {role} meter from recent InfluxDB readings. "
            f"Set {env_name} in .env to the appropriate local meter EID."
        )
    return matches[0]


def flux_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def query_rows(client: InfluxDBClient, query: str, org: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for table in client.query_api().query(query, org=org):
        for record in table.records:
            rows.append(record.values)
    return rows


def flux_or_equals(column: str, values: Iterable[str]) -> str:
    items = [value for value in values if value]
    if not items:
        return "true"
    return " or ".join(f'r.{column} == "{item}"' for item in items)


def flux_tag_filters(tags: Iterable[str]) -> list[str]:
    filters = []
    for tag in tags:
        key, sep, value = tag.partition("=")
        if not sep or not key or not value:
            raise ValueError(f"Invalid tag filter {tag!r}; expected key=value")
        filters.append(f'r.{key} == "{value}"')
    return filters


def local_midnight(value: datetime, local_tz: ZoneInfo) -> datetime:
    local = value.astimezone(local_tz)
    return datetime.combine(local.date(), time.min, tzinfo=local_tz)


def query_measurement_values(
    measurement: str,
    *,
    field: str,
    start: str,
    stop: str | None = None,
    filters: Iterable[str] = (),
    local_tz: ZoneInfo | None = None,
) -> list[TimeValueReading]:
    stop_clause = f", stop: {stop}" if stop else ""
    extra_filters = "\n".join(f"  |> filter(fn: (r) => {item})" for item in filters if item)
    query = f'''
from(bucket: "{influx_bucket()}")
  |> range(start: {start}{stop_clause})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")
{extra_filters}
  |> keep(columns: ["_time", "_value"])
  |> group()
  |> sort(columns: ["_time"])
'''
    client = influx_client()
    try:
        rows = query_rows(client, query, org=influx_org())
    finally:
        client.close()

    readings = []
    for row in rows:
        timestamp = row.get("_time")
        value = row.get("_value")
        if timestamp is None or value is None:
            continue
        if local_tz:
            timestamp = timestamp.astimezone(local_tz)
        readings.append(TimeValueReading(time=timestamp, value=float(value)))
    return readings


def query_measurement_rows(
    measurement: str,
    *,
    start: str,
    stop: str | None = None,
    fields: Iterable[str] = (),
    filters: Iterable[str] = (),
    keep_columns: Iterable[str] = (),
    sort_columns: Iterable[str] = ("_time",),
) -> list[dict[str, Any]]:
    stop_clause = f", stop: {stop}" if stop else ""
    field_filter = flux_or_equals("_field", fields)
    extra_filters = "\n".join(f"  |> filter(fn: (r) => {item})" for item in filters if item)
    keep = ""
    keep_items = [f'"{item}"' for item in keep_columns]
    if keep_items:
        keep = f"  |> keep(columns: [{', '.join(keep_items)}])\n"
    sort = ""
    sort_items = [f'"{item}"' for item in sort_columns]
    if sort_items:
        sort = f"  |> sort(columns: [{', '.join(sort_items)}])\n"
    query = f'''
from(bucket: "{influx_bucket()}")
  |> range(start: {start}{stop_clause})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => {field_filter})
{extra_filters}
{keep}  |> group()
{sort}'''
    client = influx_client()
    try:
        return query_rows(client, query, org=influx_org())
    finally:
        client.close()


def query_history_records(*, start: str = "2024-10-01T00:00:00Z", stop: str | None = None,
                          local_tz: ZoneInfo | None = None) -> list[tuple[datetime, str, float]]:
    """Deduplicate interval energy, preferring user-exported history over cloud imports."""
    fields = ("energy_produced_wh", "energy_consumed_wh", "exported_to_grid_wh", "imported_from_grid_wh")
    rows = query_measurement_rows(HISTORY_MEASUREMENT, start=start, stop=stop, fields=fields,
                                  keep_columns=("_time", "_field", "_value", "source"))
    selected = {}
    for row in rows:
        timestamp = row["_time"]
        if local_tz:
            timestamp = timestamp.astimezone(local_tz)
        key = (timestamp, row["_field"])
        priority = int(row.get("source") == "enlighten_export")
        if key not in selected or priority >= selected[key][0]:
            selected[key] = (priority, float(row["_value"]))
    return [(ts, field, value) for (ts, field), (_, value) in sorted(selected.items())]


def complete_interval_days(records: Iterable[tuple[datetime, str, float]], *, fields: Iterable[str],
                           before: date, interval_minutes: int = 15) -> tuple[dict, dict]:
    """Keep only closed local days with all finite, nonnegative, aligned intervals.

    Callers must convert timestamps to the site's timezone first. Expected intervals
    follow local midnight boundaries, including DST; duplicated wall-clock slots on
    a DST transition are deliberately excluded from fixed-slot forecasting.
    """
    required = tuple(fields)
    days = defaultdict(lambda: defaultdict(dict))
    invalid = set()
    rejected = []
    for timestamp, field, value in records:
        if field not in required or timestamp.date() >= before:
            continue
        day = timestamp.date()
        slot = (timestamp.hour * 60 + timestamp.minute) // interval_minutes
        if (timestamp.minute % interval_minutes or timestamp.second or timestamp.microsecond
                or not math.isfinite(value) or value < 0 or slot in days[day][field]):
            invalid.add(day)
            if len(rejected) < 8:
                rejected.append({"time": timestamp.isoformat(), "field": field,
                                 "value": value if math.isfinite(value) else str(value)})
        days[day][field][slot] = value
    expected = set(range(1440 // interval_minutes))
    complete = {day: {field: [values[field][slot] for slot in sorted(expected)] for field in required}
                for day, values in days.items() if day not in invalid
                and all(set(values.get(field, {})) == expected for field in required)}
    return complete, {"complete_days": len(complete), "excluded_days": len(days) - len(complete),
                      "rejected_value_examples": rejected,
                      "excluded_dates": sorted(day.isoformat() for day in days if day not in complete)}


def latest_measurement_rows(
    measurement: str,
    *,
    start: str = "-30m",
    fields: Iterable[str] = (),
    filters: Iterable[str] = (),
    group_columns: Iterable[str] = (),
    keep_columns: Iterable[str] = (),
) -> list[dict[str, Any]]:
    field_filter = flux_or_equals("_field", fields)
    extra_filters = "\n".join(f"  |> filter(fn: (r) => {item})" for item in filters if item)
    group = ""
    group_items = [f'"{item}"' for item in group_columns]
    if group_items:
        group = f"  |> group(columns: [{', '.join(group_items)}])\n"
    keep = ""
    keep_items = [f'"{item}"' for item in keep_columns]
    if keep_items:
        keep = f"  |> keep(columns: [{', '.join(keep_items)}])\n"
    query = f'''
from(bucket: "{influx_bucket()}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => {field_filter})
{extra_filters}
{group}  |> last()
{keep}'''
    client = influx_client()
    try:
        return query_rows(client, query, org=influx_org())
    finally:
        client.close()


def first_last_delta(
    measurement: str,
    *,
    field: str,
    start: str,
    stop: str,
    filters: Iterable[str] = (),
    value_scale: float = 1000.0,
) -> DeltaResult:
    extra_filters = "\n".join(f"  |> filter(fn: (r) => {item})" for item in filters if item)
    query = f'''
data = from(bucket: "{influx_bucket()}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")
{extra_filters}
  |> keep(columns: ["_time", "_value"])
  |> group()

first = data |> first() |> set(key: "point", value: "first")
last = data |> last() |> set(key: "point", value: "last")

union(tables: [first, last])
'''
    client = influx_client()
    try:
        found = {str(row.get("point")): row for row in query_rows(client, query, org=influx_org())}
    finally:
        client.close()
    if "first" not in found or "last" not in found:
        return DeltaResult(None, None, None, None, None)
    first_value = float(found["first"].get("_value") or 0.0)
    last_value = float(found["last"].get("_value") or 0.0)
    return DeltaResult(
        kwh=(last_value - first_value) / value_scale,
        first_time=found["first"].get("_time"),
        first_value=first_value,
        last_time=found["last"].get("_time"),
        last_value=last_value,
    )


def parse_hhmm(value: str) -> time:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return time(hour, minute)


def minutes_to_hhmm(minutes: float) -> str:
    rounded = int(round(minutes / SLOT_MINUTES) * SLOT_MINUTES) % (24 * 60)
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def slot_label(slot: int) -> str:
    return minutes_to_hhmm(slot * SLOT_MINUTES)


def season_for_month(month: int) -> str:
    if month in (12, 1, 2):
        return "summer"
    if month in (3, 4, 5):
        return "autumn"
    if month in (6, 7, 8):
        return "winter"
    return "spring"


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def percentile_linear(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile_linear requires at least one value")
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def percentile_rank(values: Iterable[float], value: float) -> float:
    items = list(values)
    if not items:
        return 0.0
    lower = sum(1 for item in items if item < value)
    equal = sum(1 for item in items if item == value)
    return (lower + 0.5 * equal) / len(items)


def same_time_comparisons(
    readings: Iterable[TimeValueReading],
    *,
    local_tz: ZoneInfo,
    target_date: date,
    recent_count: int = 2,
    window_minutes: int = 5,
) -> tuple[list[TimeValueReading], list[SameTimeComparison]]:
    ordered = sorted(readings, key=lambda row: row.time)
    today_readings = [row for row in ordered if row.time.astimezone(local_tz).date() == target_date]
    targets = today_readings[-recent_count:]
    window = timedelta(minutes=window_minutes)
    comparisons = []
    for target in targets:
        target_time = target.time.astimezone(local_tz)
        target_offset = target_time - local_midnight(target_time, local_tz)
        matches = []
        for row in ordered:
            row_time = row.time.astimezone(local_tz)
            if row_time.date() == target_time.date():
                continue
            row_offset = row_time - local_midnight(row_time, local_tz)
            if abs(row_offset - target_offset) <= window:
                matches.append(TimeValueReading(time=row_time, value=row.value))
        comparisons.append(SameTimeComparison(target=target, matches=matches))
    return today_readings, comparisons


def query_history_field_by_day_slot(
    field: str,
    start: str,
    local_tz: ZoneInfo,
    stop: str | None = None,
    duplicate_slot_mode: str = "last",
) -> dict[object, dict[int, float]]:
    if duplicate_slot_mode not in {"last", "sum"}:
        raise ValueError("duplicate_slot_mode must be 'last' or 'sum'")
    range_clause = f"|> range(start: {start}"
    if stop:
        range_clause += f", stop: {stop}"
    range_clause += ")"
    query = f'''
from(bucket: "{influx_bucket()}")
  {range_clause}
  |> filter(fn: (r) => r._measurement == "{HISTORY_MEASUREMENT}" and r._field == "{field}")
  |> keep(columns: ["_time", "_value"])
  |> group()
  |> sort(columns: ["_time"])
'''
    client = influx_client()
    days: dict[object, dict[int, float]] = defaultdict(dict)
    try:
        for row in query_rows(client, query, org=influx_org()):
            timestamp = row.get("_time")
            value = row.get("_value")
            if timestamp is None or value is None:
                continue
            local_time = timestamp.astimezone(local_tz)
            slot = local_time.hour * (60 // SLOT_MINUTES) + local_time.minute // SLOT_MINUTES
            if duplicate_slot_mode == "sum":
                days[local_time.date()][slot] = days[local_time.date()].get(slot, 0.0) + float(value)
            else:
                days[local_time.date()][slot] = float(value)
    finally:
        client.close()
    return days
