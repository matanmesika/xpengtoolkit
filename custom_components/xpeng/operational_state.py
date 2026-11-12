"""Explain the difference between vehicle mode, observed motion and MQTT health.

This is deliberately *not* a machine that claims to know when a car is asleep.
A silent cloud broker and a sleeping vehicle are observationally identical
without a new packet. Keep the raw cloud mode separate from interpreted state.
No Home Assistant dependencies: exercise transitions with deterministic tests.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

STALE_AFTER_SECONDS = 300


def age_seconds(timestamp: datetime | None, now: datetime) -> int | None:
    """Return the age of an actual sample, or unknown if never received."""
    if timestamp is None:
        return None
    try:
        elapsed = (now - timestamp).total_seconds()
    except (ValueError, TypeError):
        return None
    return max(0, int(elapsed))


def classify(
    *,
    connected: bool | None,
    restored: bool,
    data_age: float | None,
    parked: bool | None,
    charging: bool,
    raw_mode: Any = None,
    stale_after: float = STALE_AFTER_SECONDS,
) -> str:
    """Current evidence; neither 'Sleep' nor 'Awake' can be proved remotely."""
    if connected is False:
        return "Disconnected"
    if restored:
        return "Cached"
    if connected is None:
        return "Unknown"
    if data_age is None or data_age >= stale_after:
        return "Silent"
    if parked is False:
        return "Driving"
    if charging:
        return "Charging"
    if parked is True:
        return "Parked"
    try:
        mode = int(raw_mode)
    except (ValueError, TypeError, OverflowError):
        mode = -1
    if mode == 0:
        return "Reported parked"
    if mode == 1:
        return "Standby"
    if mode == 2:
        return "Reported driving"
    return "Unknown"


def explain(state: str) -> str:
    """Caution wording that remains valid when a broker stalls mid-drive."""
    return {
        "Disconnected": "MQTT connection down; physical vehicle state unknown",
        "Cached": "State restored from disk, not confirmed by the car",
        "Silent": "No recent meaningful telemetry; vehicle may be asleep OR cloud routing stalled",
        "Driving": "Fresh speed or gear evidence indicates driving",
        "Charging": "Fresh charging fields indicate active charging",
        "Parked": "Recent gear P evidence indicates parked",
        "Standby": "Raw XPENG cloud mode says Standby; movement not confirmed",
        "Reported parked": "Raw XPENG mode says Parked without recent gear evidence",
        "Reported driving": "Raw XPENG mode says Driving without recent gear/speed evidence",
        "Unknown": "Not enough fresh telemetry to classify",
    }.get(state, "Unknown")
