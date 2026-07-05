"""Bleak backend that routes GATT operations through a UniFi AP's bleconnd.

Home Assistant / bleak instantiate this per connection (see the HaBluetoothConnector
registered by the scanner). It resolves the AP's BleConnClient by the advertisement
`source` and delegates connect/discover/read/write/notify to that client's GATT
layer. Modeled on bleak_esphome's remote client.
"""
from __future__ import annotations

import logging
from typing import Any

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient, NotifyCallback
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak.backends.service import (
    BleakGATTService,
    BleakGATTServiceCollection,
)
from bleak.exc import BleakError

from .bleconn import BleConnClient

_LOGGER = logging.getLogger(__name__)

# Bytes of ATT/GATT header prepended to every write; the usable payload for a
# write-without-response is MTU minus this.
GATT_HEADER_SIZE = 3
_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"
# Client Characteristic Configuration Descriptor (enables notify/indicate).
CCCD_UUID = f"00002902{_BASE_UUID_SUFFIX}"

# Registry so a freshly-constructed client can find the right AP session.
_CLIENTS: dict[str, BleConnClient] = {}


def register_client(source: str, client: BleConnClient) -> None:
    """Record the BleConnClient serving a given AP source (BLE MAC, upper-case)."""
    _CLIENTS[source.upper()] = client


def unregister_client(source: str) -> None:
    """Drop the registry entry for an AP source (on config-entry unload)."""
    _CLIENTS.pop(source.upper(), None)


def _norm_uuid(u: str) -> str:
    """Normalize a 16-/32-bit shorthand or full UUID to a full lower-case UUID."""
    u = str(u).lower()
    if len(u) == 4:
        return f"0000{u}{_BASE_UUID_SUFFIX}"
    if len(u) == 8:
        return f"{u}{_BASE_UUID_SUFFIX}"
    return u


class UnifiBleakClient(BaseBleakClient):
    """Route a bleak connection through a UniFi AP (bleconnd GATT client)."""

    def __init__(self, address_or_ble_device: Any, *args: Any, **kwargs: Any) -> None:
        """Capture the peer address and the advertisement `source` (the AP MAC)
        that identifies which BleConnClient session to route through."""
        super().__init__(address_or_ble_device, *args, **kwargs)
        details = getattr(address_or_ble_device, "details", None) or {}
        self._source: str | None = details.get("source")
        self._peer_mac = self.address.replace(":", "").lower()
        self._conn_handle: int | None = None
        self._mtu = 23

    # ---- helpers -------------------------------------------------------------

    @property
    def _bc(self) -> BleConnClient:
        """Resolve the AP's BleConnClient from the registry, or raise if the AP
        session is gone (so bleak treats it as a connection failure)."""
        client = _CLIENTS.get((self._source or "").upper())
        if client is None or not client.is_scanning:
            raise BleakError(f"UniFi AP {self._source} is not available")
        return client

    def _on_disconnected(self) -> None:
        """Invoked by BleConnClient when the peer link drops: clear state and
        fire bleak's disconnected callback."""
        self._conn_handle = None
        self.services = BleakGATTServiceCollection()
        if self._disconnected_callback is not None:
            self._disconnected_callback()

    def _require_conn(self) -> int:
        """Return the active connection handle or raise if not connected."""
        if self._conn_handle is None:
            raise BleakError("not connected")
        return self._conn_handle

    @staticmethod
    def _find_cccd(characteristic: BleakGATTCharacteristic) -> int | None:
        """Return the handle of the characteristic's CCCD descriptor, if present."""
        for d in characteristic.descriptors:
            if str(d.uuid).lower() == CCCD_UUID:
                return d.handle
        return None

    # ---- BaseBleakClient interface ------------------------------------------

    @property
    def mtu_size(self) -> int:
        """Negotiated ATT MTU for the active connection."""
        return self._mtu

    @property
    def is_connected(self) -> bool:
        """True while a peer connection handle is held."""
        return self._conn_handle is not None

    async def connect(self, pair: bool = False, **kwargs: Any) -> None:
        """Open the peer connection, record the MTU, arm the disconnect callback,
        and discover the GATT database into ``self.services``."""
        bc = self._bc
        self._conn_handle = await bc.gatt_connect(self._peer_mac, timeout=self._timeout)
        self._mtu = bc.connection_mtu(self._conn_handle)
        bc.set_disconnect_callback(self._conn_handle, self._on_disconnected)
        await self._build_services()

    async def disconnect(self) -> bool:
        """Close the peer connection (best effort) and clear cached services."""
        ch, self._conn_handle = self._conn_handle, None
        if ch is not None:
            try:
                await self._bc.gatt_disconnect(ch)
            except BleakError:
                pass  # AP session already gone -> already disconnected
        self.services = BleakGATTServiceCollection()
        return True

    async def pair(self, *args: Any, **kwargs: Any) -> None:
        """Pairing is not exposed by bleconnd."""
        raise NotImplementedError("pairing is not supported via bleconnd")

    async def unpair(self) -> None:
        """Pairing is not exposed by bleconnd."""
        raise NotImplementedError("pairing is not supported via bleconnd")

    async def _build_services(self) -> BleakGATTServiceCollection:
        """Discover the peer's GATT DB via bleconnd and build the bleak service
        collection (services -> characteristics -> descriptors)."""
        mtu = self._mtu

        def get_max_write_without_response() -> int:
            """Usable payload for a write-without-response at the current MTU."""
            return mtu - GATT_HEADER_SIZE

        services = BleakGATTServiceCollection()
        db = await self._bc.gatt_discover(self._conn_handle)
        for s in db:
            svc = BleakGATTService({"handle": s["handle"]}, s["handle"],
                                   _norm_uuid(s["uuid"]))
            services.add_service(svc)
            for c in s["characteristics"]:
                char = BleakGATTCharacteristic(
                    {"handle": c["handle"]}, c["handle"], _norm_uuid(c["uuid"]),
                    list(c["properties"]), get_max_write_without_response, svc)
                services.add_characteristic(char)
                for d in c["descriptors"]:
                    services.add_descriptor(BleakGATTDescriptor(
                        {"handle": d["handle"]}, d["handle"],
                        _norm_uuid(d["uuid"]), char))
        if not db:
            raise BleakError("service discovery returned no services")
        self.services = services
        return services

    async def read_gatt_char(self, characteristic: BleakGATTCharacteristic,
                             **kwargs: Any) -> bytearray:
        """Read a characteristic value."""
        return bytearray(await self._bc.gatt_read_char(
            self._require_conn(), characteristic.handle))

    async def read_gatt_descriptor(self, descriptor: BleakGATTDescriptor,
                                   **kwargs: Any) -> bytearray:
        """Read a descriptor value."""
        return bytearray(await self._bc.gatt_read_desc(
            self._require_conn(), descriptor.handle))

    async def write_gatt_char(self, characteristic: BleakGATTCharacteristic,
                              data, response: bool) -> None:
        """Write a characteristic value (with or without response)."""
        await self._bc.gatt_write_char(
            self._require_conn(), characteristic.handle, bytes(data), response)

    async def write_gatt_descriptor(self, descriptor: BleakGATTDescriptor,
                                    data) -> None:
        """Write a descriptor value."""
        await self._bc.gatt_write_desc(
            self._require_conn(), descriptor.handle, bytes(data))

    async def start_notify(self, characteristic: BleakGATTCharacteristic,
                           callback: NotifyCallback, **kwargs: Any) -> None:
        """Subscribe to notifications/indications on a characteristic by writing
        its CCCD; forwarded values are delivered to ``callback`` as bytearrays."""
        cccd = self._find_cccd(characteristic)
        if cccd is None:
            raise BleakError(f"no CCCD for characteristic {characteristic.uuid}")
        indicate = ("indicate" in characteristic.properties
                    and "notify" not in characteristic.properties)

        def _cb(data: bytes) -> None:
            """Adapt bleconnd's bytes payload to bleak's bytearray callback."""
            callback(bytearray(data))

        await self._bc.gatt_start_notify(
            self._require_conn(), characteristic.handle, cccd, _cb, indicate)

    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        """Unsubscribe from a characteristic by clearing its CCCD."""
        cccd = self._find_cccd(characteristic)
        if cccd is not None:
            await self._bc.gatt_stop_notify(
                self._require_conn(), characteristic.handle, cccd)
