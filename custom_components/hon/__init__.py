import logging
from pathlib import Path
from typing import Any

import voluptuous as vol  # type: ignore[import-untyped]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import config_validation as cv, aiohttp_client
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyhon import Hon

from .const import DOMAIN, PLATFORMS, MOBILE_ID, CONF_REFRESH_TOKEN

_LOGGER = logging.getLogger(__name__)

HON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema(vol.All(cv.ensure_list, [HON_SCHEMA]))},
    extra=vol.ALLOW_EXTRA,
)


_COORDINATOR_LOGGER = logging.getLogger(f"{__name__}.coordinator")
_COORDINATOR_LOGGER.setLevel(logging.INFO)


class HonDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """DataUpdateCoordinator wyciszający powtarzalny spam debugowy w logach."""

    def _set_updated_data(self, data: dict[str, Any]) -> None:
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()

    def async_set_updated_data(self, data: dict[str, Any]) -> None:
        self._set_updated_data(data)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = aiohttp_client.async_get_clientsession(hass)
    if (config_dir := hass.config.config_dir) is None:
        raise ValueError("Missing Config Dir")
    hon = await Hon(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        mobile_id=MOBILE_ID,
        session=session,
        test_data_path=Path(config_dir),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN, ""),
    ).create()

    # Save the new refresh token
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_REFRESH_TOKEN: hon.api.auth.refresh_token}
    )

    coordinator: DataUpdateCoordinator[dict[str, Any]] = HonDataUpdateCoordinator(
        hass, _COORDINATOR_LOGGER, name=DOMAIN
    )
    hon.subscribe_updates(coordinator.async_set_updated_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.unique_id] = {"hon": hon, "coordinator": coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_data = hass.data[DOMAIN].get(entry.unique_id, {})
    for debouncer in entry_data.get("debouncers", {}).values():
        debouncer.cancel()
    refresh_token = entry_data["hon"].api.auth.refresh_token

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_REFRESH_TOKEN: refresh_token}
    )
    unload = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload:
        hass.data[DOMAIN].pop(entry.unique_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unload
