"""SSH tunnel transport and shared keypair management.

The integration owns a single Ed25519 keypair (persisted in HA storage). The user
provisions its public key once into UniFi's Device SSH Authentication, which pushes
it to all adopted devices (APs and the gateway). We then open an SSH connection to
each AP — optionally hopping through the UDM gateway — and forward a direct-tcpip
channel to the AP's loopback bleconnd. No local ports, no external autossh.

Host keys are pinned trust-on-first-use: the config flow probes with verification
off, records the keys each server presented (`observed_host_keys`), and stores them
in the entry; later connections pass them back via `host_keys` and are verified.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import asyncssh

from .bleconn import Transport

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_STORE_KEY = "unifi_ble_ssh_key"
_STORE_VERSION = 1
_KEY_COMMENT = "home-assistant-unifi-ble"


async def async_get_keypair(hass: "HomeAssistant") -> tuple[asyncssh.SSHKey, str]:
    """Return (private key object, public key in OpenSSH form), generating once."""
    # Local import keeps this module importable (and testable) without Home Assistant.
    from homeassistant.helpers.storage import Store

    store = Store(hass, _STORE_VERSION, _STORE_KEY)
    data = await store.async_load()
    if data and data.get("private_key"):
        key = asyncssh.import_private_key(data["private_key"])
    else:
        key = asyncssh.generate_private_key("ssh-ed25519", comment=_KEY_COMMENT)
        await store.async_save({"private_key": key.export_private_key().decode()})
    return key, key.export_public_key().decode().strip()


class SshTunnelTransport(Transport):
    """Open an SSH channel to an AP's loopback bleconnd, optionally via a jump host."""

    def __init__(self, ap_host: str, ap_user: str, bleconn_port: int,
                 private_key: asyncssh.SSHKey, *,
                 jump_host: str | None = None, jump_user: str | None = None,
                 host_keys: dict[str, str] | None = None,
                 ssh_port: int = 22, connect_timeout: float = 15.0):
        self._ap_host = ap_host
        self._ap_user = ap_user
        self._port = bleconn_port
        self._key = private_key
        self._jump_host = jump_host
        self._jump_user = jump_user or ap_user
        self._ssh_port = ssh_port
        self._timeout = connect_timeout
        self._conn: asyncssh.SSHClientConnection | None = None
        self._jump_conn: asyncssh.SSHClientConnection | None = None
        # host -> "keytype base64 [comment]" of the pinned host key; None entries
        # (or no dict at all) mean trust-on-first-use for that host.
        self.host_keys = host_keys or {}
        # Keys actually presented by each host during connect(), same format.
        self.observed_host_keys: dict[str, str] = {}

    def _known_hosts(self, host: str):
        line = self.host_keys.get(host)
        if not line:
            return None                           # not pinned yet: first contact
        pattern = host if self._ssh_port == 22 else f"[{host}]:{self._ssh_port}"
        return asyncssh.import_known_hosts(f"{pattern} {line}\n")

    def _record_host_key(self, host: str,
                         conn: asyncssh.SSHClientConnection) -> None:
        key = conn.get_server_host_key()
        if key is not None:
            self.observed_host_keys[host] = key.export_public_key().decode().strip()

    async def connect(self):
        tunnel = None
        if self._jump_host:
            self._jump_conn = await asyncssh.connect(
                self._jump_host, port=self._ssh_port, username=self._jump_user,
                client_keys=[self._key],
                known_hosts=self._known_hosts(self._jump_host),
                connect_timeout=self._timeout)
            self._record_host_key(self._jump_host, self._jump_conn)
            tunnel = self._jump_conn
        self._conn = await asyncssh.connect(
            self._ap_host, port=self._ssh_port, username=self._ap_user,
            client_keys=[self._key],
            known_hosts=self._known_hosts(self._ap_host), tunnel=tunnel,
            connect_timeout=self._timeout)
        self._record_host_key(self._ap_host, self._conn)
        # direct-tcpip channel to the AP's own loopback bleconnd
        return await self._conn.open_connection("127.0.0.1", self._port)

    async def disconnect(self) -> None:
        for conn in (self._conn, self._jump_conn):
            if conn is not None:
                conn.close()
                try:
                    await conn.wait_closed()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    pass
        self._conn = self._jump_conn = None

    def describe(self) -> str:
        via = f" via {self._jump_user}@{self._jump_host}" if self._jump_host else ""
        return f"{self._ap_user}@{self._ap_host}:{self._port}{via}"
