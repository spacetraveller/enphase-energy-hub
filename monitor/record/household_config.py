"""Generic, optional household features configured through environment variables.

The shareable defaults assume no EV or charger telemetry. Keep household-specific
values in the local, ignored environment file rather than in tracked code.
"""
import os
from datetime import date, time


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def household_ev_enabled() -> bool:
    return _enabled("HOUSEHOLD_EV_ENABLED")


def ev_charger_telemetry_enabled() -> bool:
    return household_ev_enabled() and _enabled("EV_CHARGER_TELEMETRY_ENABLED")


def ev_history_start_date() -> date | None:
    """Optional date when the current EV-era load regime began for this home."""
    raw = os.getenv("HOUSEHOLD_EV_HISTORY_START_DATE", "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("HOUSEHOLD_EV_HISTORY_START_DATE must use YYYY-MM-DD") from exc


def green_charge_surplus_kw() -> float:
    value = float(os.getenv("GREEN_CHARGE_SURPLUS_KW", "2.0"))
    if value < 0:
        raise ValueError("GREEN_CHARGE_SURPLUS_KW must be non-negative")
    return value


def hot_water_estimate_window() -> tuple[int, int] | None:
    """Return the configured quarter-hour slot range, or None if unset.

    Configure both HOUSEHOLD_HOT_WATER_ESTIMATE_START and
    HOUSEHOLD_HOT_WATER_ESTIMATE_END as HH:MM local clock times. The end is
    exclusive and must be later on the same day than the start.
    """
    start_raw = os.getenv("HOUSEHOLD_HOT_WATER_ESTIMATE_START", "").strip()
    end_raw = os.getenv("HOUSEHOLD_HOT_WATER_ESTIMATE_END", "").strip()
    if not start_raw and not end_raw:
        return None
    if not start_raw or not end_raw:
        raise ValueError(
            "HOUSEHOLD_HOT_WATER_ESTIMATE_START and "
            "HOUSEHOLD_HOT_WATER_ESTIMATE_END must be configured together"
        )

    def parse_slot(name: str, raw: str) -> int:
        if len(raw) != 5 or raw[2] != ":" or not (raw[:2] + raw[3:]).isdigit():
            raise ValueError(f"{name} must use HH:MM format")
        try:
            parsed = time.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must use HH:MM format") from exc
        if parsed.second or parsed.microsecond or parsed.minute % 15:
            raise ValueError(f"{name} must be aligned to a 15-minute boundary")
        return parsed.hour * 4 + parsed.minute // 15

    start_slot = parse_slot("HOUSEHOLD_HOT_WATER_ESTIMATE_START", start_raw)
    end_slot = parse_slot("HOUSEHOLD_HOT_WATER_ESTIMATE_END", end_raw)
    if end_slot <= start_slot:
        raise ValueError(
            "HOUSEHOLD_HOT_WATER_ESTIMATE_END must be later than start on the same day"
        )
    return start_slot, end_slot
