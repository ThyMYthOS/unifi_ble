"""Config flow for UniFi AP BLE Proxy.

One form per AP: it shows the integration's public SSH key (as a read-only,
copy-able field) to provision into UniFi Device SSH Authentication, and collects
the AP's connection details, validating them by tunneling in and handshaking with
bleconnd. On failure the entered values are retained so they can be corrected.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

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

# Display-only field carrying the public key; never stored in the entry.
FIELD_PUBLIC_KEY = "public_key"


class UnifiBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one UniFi AP per entry, tunneled over SSH with the shared keypair."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize per-flow state (the public key is loaded lazily)."""
        self._public_key: str = ""

    def _build_schema(self) -> vol.Schema:
        """Build the form schema, including the read-only public-key field (which
        the HA frontend renders with a copy-to-clipboard icon)."""
        return vol.Schema(
            {
                vol.Optional(FIELD_PUBLIC_KEY, default=self._public_key): TextSelector(
                    TextSelectorConfig(multiline=True, read_only=True)
                ),
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
                vol.Optional(CONF_JUMP_HOST, default=""): str,
                vol.Optional(CONF_JUMP_USERNAME, default=""): str,
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show/handle the single setup form.

        Generates (once) and exposes the SSH public key, and on submit validates
        the connection by opening the SSH tunnel and handshaking with bleconnd.
        On error the form is re-shown with the user's values preserved.
        """
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
                    # Persist connection fields only (drop blanks and the
                    # display-only public key); pin the observed host keys.
                    data = {
                        k: v for k, v in user_input.items()
                        if k != FIELD_PUBLIC_KEY and v != ""
                    }
                    data[CONF_HOST_KEYS] = transport.observed_host_keys
                    return self.async_create_entry(
                        title=f"UniFi AP {user_input[CONF_HOST]}", data=data)

        schema = self._build_schema()
        if user_input is not None:
            # Re-show with the user's previous entries preserved.
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
