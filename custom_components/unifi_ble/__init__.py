"""UniFi AP BLE Proxy: expose an AP's BLE radio as a Home Assistant remote scanner."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.bluetooth import async_register_scanner
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .bleconn import Advertisement, BleConnClient, probe_transport
from .const import (
    CONF_HOST,
    CONF_HOST_KEYS,
    CONF_JUMP_HOST,
    CONF_JUMP_USERNAME,
    CONF_PORT,
    CONF_SCAN_PHYS,
    CONF_USERNAME,
    DEFAULT_SCAN_PHYS,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .scanner import UnifiBleScanner
from .ssh import SshTunnelTransport, async_get_keypair

_LOGGER = logging.getLogger(__name__)


def _describe_adv(adv: Advertisement) -> str:
    """Compact one-line rendering of an advertisement for debug logging."""
    parts = [f"{adv.address} rssi={adv.rssi}"]
    if adv.local_name:
        parts.append(f"name={adv.local_name!r}")
    if adv.tx_power is not None:
        parts.append(f"tx={adv.tx_power}")
    if adv.service_uuids:
        parts.append(f"svc={adv.service_uuids}")
    if adv.service_data:
        parts.append("svcdata={"
                     + ", ".join(f"{k}:{v.hex()}" for k, v in adv.service_data.items())
                     + "}")
    if adv.manufacturer_data:
        parts.append("mfr={"
                     + ", ".join(f"0x{k:04x}:{v.hex()}"
                                 for k, v in adv.manufacturer_data.items())
                     + "}")
    if adv.connectable:
        parts.append("connectable")
    return "  ".join(parts)


class UnifiBleRuntime:
    """Holds the per-entry client, scanner and background task."""

    def __init__(self, client: BleConnClient, unregister, unsetup,
                 task: asyncio.Task) -> None:
        self.client = client
        self.unregister = unregister
        self.unsetup = unsetup
        self.task = task


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, 8383)
    scan_phys = tuple(entry.data.get(CONF_SCAN_PHYS, DEFAULT_SCAN_PHYS))

    key, _public = await async_get_keypair(hass)
    transport = SshTunnelTransport(
        host,
        entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        port,
        key,
        jump_host=entry.data.get(CONF_JUMP_HOST) or None,
        jump_user=entry.data.get(CONF_JUMP_USERNAME) or None,
        host_keys=entry.data.get(CONF_HOST_KEYS),
    )

    # Fail fast so an unreachable AP shows up in the UI and HA retries with backoff.
    try:
        await probe_transport(transport)
    except Exception as exc:
        raise ConfigEntryNotReady(
            f"cannot reach bleconnd on {host}: {exc}") from exc

    # Entries created before host-key pinning: adopt the keys seen just now.
    if not entry.data.get(CONF_HOST_KEYS) and transport.observed_host_keys:
        transport.host_keys = dict(transport.observed_host_keys)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST_KEYS: transport.host_keys})

    # Entries created before host-based naming were titled "UniFi AP <mac>";
    # switch those to the hostname (user-customized titles are left alone).
    if entry.unique_id and entry.title.lower() == f"unifi ap {entry.unique_id}".lower():
        hass.config_entries.async_update_entry(entry, title=f"UniFi AP {host}")

    # entry.unique_id is the AP's BLE MAC (set in the config flow) -> stable source.
    source = (entry.unique_id or f"{host}:{port}").upper()
    scanner = UnifiBleScanner(source, entry.title, connector=None, connectable=False)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, dr.format_mac(source))},
        identifiers={(DOMAIN, source)},
        manufacturer="Ubiquiti",
        model="UniFi AP (BLE proxy)",
        name=entry.title,
    )

    seen: set[str] = set()

    def on_advertisement(adv: Advertisement) -> None:
        scanner.push(adv)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("%s: adv %s", entry.title, _describe_adv(adv))
        if adv.address not in seen:
            seen.add(adv.address)
            # First advert confirms the pipeline; then a heartbeat every 25 devices.
            if len(seen) == 1 or len(seen) % 25 == 0:
                _LOGGER.info("%s: forwarding advertisements, %d unique device(s) "
                             "(latest %s)", entry.title, len(seen), adv.address)

    last_state: str | None = None

    def on_state(state: str) -> None:
        nonlocal last_state
        if state == last_state:
            _LOGGER.debug("%s: still %s", entry.title, state)
        elif state.startswith("disconnected"):
            _LOGGER.warning("%s: %s (will keep retrying)", entry.title, state)
        else:
            _LOGGER.info("%s: %s", entry.title, state)
        last_state = state

    client = BleConnClient(
        transport, scan_phys=scan_phys,
        on_advertisement=on_advertisement, on_state=on_state,
    )

    unsetup = scanner.async_setup()
    unregister = async_register_scanner(hass, scanner)
    task = entry.async_create_background_task(
        hass, client.run(), name=f"unifi_ble[{source}]")

    entry.runtime_data = UnifiBleRuntime(client, unregister, unsetup, task)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime: UnifiBleRuntime = entry.runtime_data
    # Cancellation is the shutdown path: run()'s finally closes the transport.
    runtime.task.cancel()
    await asyncio.gather(runtime.task, return_exceptions=True)
    runtime.unregister()
    runtime.unsetup()
    return True
