"""Device tracker platform for the XPENG integration."""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import XPengDataCoordinator
from .entity import XPengEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XPENG device tracker."""
    coordinator: XPengDataCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities([XPengDeviceTracker(coordinator)])


class XPengDeviceTracker(XPengEntity, TrackerEntity):
    """Represent the XPENG vehicle location as a device tracker."""

    _attr_has_entity_name = True
    _attr_translation_key = "location"
    _attr_icon = "mdi:car"

    def __init__(self, coordinator: XPengDataCoordinator) -> None:
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._attr_unique_id = f"xpeng_{self._vin}_location"

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Return latitude."""
        val = self._get_nested_value("msg_content.interval.drive.latitude")
        if val is not None and val != 0:
            return float(val)
        return None

    @property
    def longitude(self) -> float | None:
        """Return longitude."""
        val = self._get_nested_value("msg_content.interval.drive.longitude")
        if val is not None and val != 0:
            return float(val)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        """Return extra attributes."""
        speed_raw = self._get_nested_value("msg_content.interval.drive.speed")
        angle = self._get_nested_value("msg_content.interval.drive.angle")
        coord_type = self._get_nested_value("msg_content.interval.drive.coordType")

        speed = None
        if speed_raw is not None:
            try:
                speed = round(float(speed_raw), 1)
            except (ValueError, TypeError):
                pass

        return {
            "speed": speed,
            "bearing": angle,
            "coord_type": coord_type,
        }
