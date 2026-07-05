"""Diagnostic sensors: active GATT connections and devices in range per AP."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities) -> None:
    """Set up the diagnostic sensors for this AP."""
    runtime = entry.runtime_data
    async_add_entities([
        UnifiBleDiagSensor(
            runtime.coordinator, runtime.source, "active_connections",
            "Active connections", "connections",
            lambda d: d.get("active_connections", 0)),
        UnifiBleDiagSensor(
            runtime.coordinator, runtime.source, "devices_in_range",
            "Devices in range", "devices",
            lambda d: d.get("devices_in_range", 0)),
    ])


class UnifiBleDiagSensor(CoordinatorEntity, SensorEntity):
    """A diagnostic sensor reading one value from the coordinator snapshot."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, source: str, key: str, name: str,
                 unit: str, extract: Callable[[dict], int]) -> None:
        """Bind to the coordinator and configure this sensor's value getter."""
        super().__init__(coordinator)
        self._extract = extract
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{source}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, source)})

    @property
    def native_value(self) -> int:
        """Current value pulled from the latest diagnostics snapshot."""
        return self._extract(self.coordinator.data or {})
