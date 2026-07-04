"""Config flow for UniFi AP BLE Proxy.

Two steps: first show the integration's public SSH key so the user can provision it
into UniFi Device SSH Authentication; then collect each AP's connection details and
validate by tunneling in and handshaking with bleconnd.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .bleconn import probe_transport
from .const import (
    CONF_HOST,
    CONF_HOST_KEYS,
    CONF_JUMP_HOST,
    CONF_JUMP_USERNAME,
    CONF_PORT,
    CONF_USERNAME,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .ssh import SshTunnelTransport, async_get_keypair

CONNECT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Optional(CONF_JUMP_HOST, default=""): str,
        vol.Optional(CONF_JUMP_USERNAME, default=""): str,
    }
)


class UnifiBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one UniFi AP per entry, tunneled over SSH with the shared keypair."""

    VERSION = 1

    def __init__(self) -> None:
        self._public_key: str | None = None

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        # Ensure the keypair exists and show its public key on the first screen.
        key, self._public_key = await async_get_keypair(self.hass)

        errors: dict[str, str] = {}
        if user_input is not None:
            transport = SshTunnelTransport(
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PORT],
                key,
                jump_host=user_input.get(CONF_JUMP_HOST) or None,
                jump_user=user_input.get(CONF_JUMP_USERNAME) or None,
            )
            try:
                info = await probe_transport(transport)
            except Exception:  # noqa: BLE001 - surface any failure as cannot_connect
                errors["base"] = "cannot_connect"
            else:
                mac = info.get("mac")
                if not mac:
                    errors["base"] = "no_adapter"
                else:
                    await self.async_set_unique_id(mac.upper())
                    self._abort_if_unique_id_configured()
                    data = {k: v for k, v in user_input.items() if v != ""}
                    # Pin the host keys seen on first contact (trust-on-first-use);
                    # every later connection verifies against them.
                    data[CONF_HOST_KEYS] = transport.observed_host_keys
                    return self.async_create_entry(
                        title=f"UniFi AP {user_input[CONF_HOST]}", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=CONNECT_SCHEMA,
            errors=errors,
            description_placeholders={"public_key": self._public_key},
        )
