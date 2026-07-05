"""Diagnostic binary sensor: whether the AP's bleconnd session is up."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities) -> None:
    """Set up the connectivity diagnostic binary sensor for this AP."""
    runtime = entry.runtime_data
    async_add_entities([UnifiBleConnectivity(runtime.coordinator, runtime.source)])


class UnifiBleConnectivity(CoordinatorEntity, BinarySensorEntity):
    """On while the SSH + bleconnd session for this AP is established."""

    _attr_has_entity_name = True
    _attr_name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, source: str) -> None:
        """Bind to the diagnostics coordinator and this AP's device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{source}_connectivity"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, source)})

    @property
    def is_on(self) -> bool:
        """True when the bleconnd session is connected and scanning."""
        return bool((self.coordinator.data or {}).get("connected"))
