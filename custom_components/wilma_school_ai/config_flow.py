"""
config_flow.py — UI configuration flow for the Wilma integration
=================================================================
PURPOSE
    Replaces YAML-based configuration. The user sets up the integration
    through Settings → Devices & Services → Add Integration → Wilma.

FLOW
    Step 1 (user):
        User enters base URL, username, password.
        We test login and auto-discover children via get_children().
        On success a config entry is created and children are stored in it.

    Options flow (post-setup, via the Configure button):
        User can change the poll interval without re-entering credentials.

ERROR KEYS (shown in the UI via strings.json)
    cannot_connect   — network/HTTP error reaching Wilma
    invalid_auth     — login succeeded HTTP-wise but credentials were wrong
    no_children      — login worked but no children found on the account
    unknown          — unexpected exception
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .client import WilmaClient
from .const import (
    CONF_BASE_URL,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_CHILDREN,
    CONF_SENDER_FILTERS,
    CONF_MESSAGE_LIMIT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_MESSAGE_LIMIT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class WilmaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                children = await self.hass.async_add_executor_job(
                    _test_credentials,
                    user_input[CONF_BASE_URL],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except RuntimeError as err:
                _LOGGER.debug("Wilma auth error: %s", err)
                errors["base"] = "invalid_auth"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error connecting to Wilma: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not children:
                    errors["base"] = "no_children"
                else:
                    # Use base_url as the unique ID so the same tenant can't
                    # be added twice.
                    await self.async_set_unique_id(
                        user_input[CONF_BASE_URL].rstrip("/").lower()
                    )
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=user_input[CONF_BASE_URL].rstrip("/"),
                        data={
                            CONF_BASE_URL:      user_input[CONF_BASE_URL].rstrip("/"),
                            CONF_USERNAME:      user_input[CONF_USERNAME],
                            CONF_PASSWORD:      user_input[CONF_PASSWORD],
                            CONF_CHILDREN:      children,
                            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> WilmaOptionsFlow:
        return WilmaOptionsFlow()


class WilmaOptionsFlow(config_entries.OptionsFlow):
    """Handle options (poll interval) after the integration is set up."""

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        def _opt(key, default):
            return self.config_entry.options.get(
                key, self.config_entry.data.get(key, default)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=_opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): int,
                    vol.Optional(CONF_SENDER_FILTERS, default=_opt(CONF_SENDER_FILTERS, "")): str,
                    vol.Required(CONF_MESSAGE_LIMIT, default=_opt(CONF_MESSAGE_LIMIT, DEFAULT_MESSAGE_LIMIT)): int,
                }
            ),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _test_credentials(base_url: str, username: str, password: str) -> list[dict]:
    """Blocking: login and return discovered children. Runs in executor."""
    client = WilmaClient(base_url, username, password)
    client.login()
    return client.get_children()
