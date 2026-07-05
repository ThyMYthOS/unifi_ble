"""Remote scanner bridging a UniFi AP's bleconnd scan into Home Assistant."""
from __future__ import annotations

import logging

from habluetooth import BaseHaRemoteScanner

from homeassistant.components.bluetooth import MONOTONIC_TIME

from .bleconn import Advertisement

_LOGGER = logging.getLogger(__name__)


class UnifiBleScanner(BaseHaRemoteScanner):
    """Feeds advertisements from one UniFi AP into HA's Bluetooth stack.

    Registered connectable: advertisements are forwarded here, and connections to
    the devices it sees are routed through ``client.UnifiBleakClient`` via the
    ``HaBluetoothConnector`` attached at registration.
    """

    def push(self, adv: Advertisement) -> None:
        """Forward one parsed advertisement into habluetooth."""
        self._async_on_advertisement(
            address=adv.address,
            rssi=adv.rssi,
            local_name=adv.local_name,
            service_uuids=adv.service_uuids,
            service_data=adv.service_data,
            manufacturer_data=adv.manufacturer_data,
            tx_power=adv.tx_power,
            details={"address_type": adv.address_type},
            advertisement_monotonic_time=MONOTONIC_TIME(),
        )
