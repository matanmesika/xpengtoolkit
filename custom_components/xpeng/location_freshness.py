"""Distinguish a live GPS fix from an old coordinate carried in cached state.

No Home Assistant import: the decision can be tested with synthetic MQTT packets.
"""

from __future__ import annotations

from datetime import datetime
import math

#: A location older than 15 minutes is last-known, not live telemetry.
#: The car can sleep longer while parked; callers should expose it as unavailable
#: rather than claim the parked coordinate is confirmed current.
LOCATION_MAX_AGE_SECONDS = 15 * 60


def has_fresh_position(content: object) -> bool:
    """Whether THIS MQTT message supplies a usable GPS fix.

    Inspect the original payload, never the merged cached frame. A door/seat
    partial can retain yesterday's interval.drive after merging without
    carrying any fresh coordinates itself.
    """
    if not isinstance(content, dict):
        return False
    interval = content.get("interval")
    if not isinstance(interval, dict):
        return False
    drive = interval.get("drive")
    if not isinstance(drive, dict):
        return False
    try:
        latitude = float(drive["latitude"])
        longitude = float(drive["longitude"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return False
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return False
    # Existing tracker treats either zero axis as an absent fix; do not stamp
    # such a frame fresh until the tracker itself supports those coordinates.
    return latitude != 0 and longitude != 0


def location_age_seconds(last_fix: datetime | None, now: datetime) -> float | None:
    """Age of the last actual GPS-bearing MQTT frame, or None."""
    if last_fix is None:
        return None
    try:
        return (now - last_fix).total_seconds()
    except (TypeError, ValueError):
        return None


def location_is_fresh(
    last_fix: datetime | None,
    now: datetime,
    *,
    restored: bool = False,
    max_age_seconds: int = LOCATION_MAX_AGE_SECONDS,
) -> bool:
    """A cached location is never fresh; timeout uses wall clock while idle."""
    if restored:
        return False
    age = location_age_seconds(last_fix, now)
    return age is not None and 0 <= age < max_age_seconds
