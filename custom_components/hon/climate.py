import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
)
from homeassistant.components.climate.const import (
    SWING_OFF,
    SWING_BOTH,
    SWING_VERTICAL,
    SWING_HORIZONTAL,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    UnitOfTemperature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant
from pyhon.appliance import HonAppliance
from pyhon.parameter.range import HonParameterRange

from .const import HON_HVAC_MODE, HON_FAN, DOMAIN, HON_HVAC_PROGRAM
from .entity import HonEntity

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_DELAY = 2.0


@dataclass(frozen=True)
class HonACClimateEntityDescription(ClimateEntityDescription):
    pass


@dataclass(frozen=True)
class HonClimateEntityDescription(ClimateEntityDescription):
    mode: HVACMode = HVACMode.AUTO


CLIMATES: dict[
    str, tuple[HonACClimateEntityDescription | HonClimateEntityDescription, ...]
] = {
    "AC": (
        HonACClimateEntityDescription(
            key="settings",
            name="Air Conditioner",
            icon="mdi:air-conditioner",
            translation_key="air_conditioner",
        ),
    ),
    "REF": (
        HonClimateEntityDescription(
            key="settings.tempSelZ1",
            mode=HVACMode.COOL,
            name="Fridge",
            icon="mdi:thermometer",
            translation_key="fridge",
        ),
        HonClimateEntityDescription(
            key="settings.tempSelZ2",
            mode=HVACMode.COOL,
            name="Freezer",
            icon="mdi:snowflake-thermometer",
            translation_key="freezer",
        ),
        HonClimateEntityDescription(
            key="settings.tempSelZ3",
            mode=HVACMode.COOL,
            name="MyZone",
            icon="mdi:thermometer",
            translation_key="my_zone",
        ),
    ),
    "OV": (
        HonClimateEntityDescription(
            key="settings.tempSel",
            mode=HVACMode.HEAT,
            name="Oven",
            icon="mdi:thermometer",
            translation_key="oven",
        ),
    ),
    "WC": (
        HonClimateEntityDescription(
            key="settings.tempSel",
            mode=HVACMode.COOL,
            name="Wine Cellar",
            icon="mdi:thermometer",
            translation_key="wine",
        ),
        HonClimateEntityDescription(
            key="settings.tempSelZ2",
            mode=HVACMode.COOL,
            name="Wine Cellar",
            icon="mdi:thermometer",
            translation_key="wine",
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities = []
    entity: HonClimateEntity | HonACClimateEntity
    for device in hass.data[DOMAIN][entry.unique_id]["hon"].appliances:
        for description in CLIMATES.get(device.appliance_type, []):
            if isinstance(description, HonACClimateEntityDescription):
                if description.key not in list(device.commands):
                    continue
                entity = HonACClimateEntity(hass, entry, device, description)
            elif isinstance(description, HonClimateEntityDescription):
                if description.key not in device.available_settings:
                    continue
                entity = HonClimateEntity(hass, entry, device, description)
            else:
                continue  # type: ignore[unreachable]
            entities.append(entity)
    async_add_entities(entities)


class HonACClimateEntity(HonEntity, ClimateEntity):
    entity_description: HonACClimateEntityDescription
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: HonAppliance,
        description: HonACClimateEntityDescription,
    ) -> None:
        super().__init__(hass, entry, device, description)

        self._debounce_task: asyncio.Task[None] | None = None
        self._attr_target_temperature: float | None = None
        self._attr_fan_mode: str | None = None
        self._attr_swing_mode: str | None = None

        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._set_temperature_bound()

        self._attr_hvac_modes = [HVACMode.OFF]
        for mode in device.settings["settings.machMode"].values:
            self._attr_hvac_modes.append(HON_HVAC_MODE[int(mode)])
        self._attr_preset_modes = []
        for mode in device.settings["startProgram.program"].values:
            self._attr_preset_modes.append(mode)
        self._attr_swing_modes = [
            SWING_OFF,
            SWING_VERTICAL,
            SWING_HORIZONTAL,
            SWING_BOTH,
        ]
        self._attr_supported_features = (
            ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            | ClimateEntityFeature.PRESET_MODE
        )

        self._handle_coordinator_update(update=False)

    def _cancel_debounce(self) -> None:
        """Cancel any pending debounced settings command."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None

    def _schedule_debounce_settings(self) -> None:
        """Schedule or reschedule debounced settings command send."""
        self._cancel_debounce()
        self._debounce_task = self.hass.async_create_task(
            self._async_debounced_send_settings()
        )

    async def _async_debounced_send_settings(self) -> None:
        """Wait for debounce delay and send accumulated settings."""
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
        except asyncio.CancelledError:
            return

        # Ensure current machMode and onOffStatus match actual device state
        if "settings.machMode" in self._device.settings:
            current_mach = self._device.get("machMode")
            if current_mach is not None:
                self._device.settings["settings.machMode"].value = str(int(current_mach))
        if "settings.onOffStatus" in self._device.settings:
            current_onoff = self._device.get("onOffStatus")
            if current_onoff is not None:
                self._device.settings["settings.onOffStatus"].value = str(int(current_onoff))

        try:
            await self._device.commands["settings"].send()
        except Exception as exc:
            _LOGGER.error("Nie udało się wysłać zakumulowanych ustawień AC: %s", exc)
            self._device.sync_params_to_command("settings")
            self._attr_target_temperature = None
            self._attr_fan_mode = None
            self._attr_swing_mode = None
            self.schedule_update_ha_state()
            return

        params = self._device.attributes.get("parameters", {})
        temp_setting = self._device.settings.get("settings.tempSel")
        if temp_setting and "tempSel" in params:
            with suppress(ValueError, TypeError):
                params["tempSel"].update(str(int(float(temp_setting.value))), shield=True)

        wind_setting = self._device.settings.get("settings.windSpeed")
        if wind_setting and "windSpeed" in params:
            with suppress(ValueError, TypeError):
                params["windSpeed"].update(str(int(wind_setting.value)), shield=True)

        h_setting = self._device.settings.get("settings.windDirectionHorizontal")
        if h_setting and "windDirectionHorizontal" in params:
            with suppress(ValueError, TypeError):
                params["windDirectionHorizontal"].update(str(h_setting.value), shield=True)

        v_setting = self._device.settings.get("settings.windDirectionVertical")
        if v_setting and "windDirectionVertical" in params:
            with suppress(ValueError, TypeError):
                params["windDirectionVertical"].update(str(v_setting.value), shield=True)

        self._attr_target_temperature = None
        self._attr_fan_mode = None
        self._attr_swing_mode = None

        self.coordinator.async_set_updated_data({})
        self.schedule_update_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        self._cancel_debounce()
        await super().async_will_remove_from_hass()

    def _set_temperature_bound(self) -> None:
        temperature = self._device.settings["settings.tempSel"]
        if not isinstance(temperature, HonParameterRange):
            raise ValueError
        self._attr_max_temp = temperature.max
        self._attr_target_temperature_step = temperature.step
        self._attr_min_temp = temperature.min

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        if self._attr_target_temperature is not None:
            return self._attr_target_temperature
        return self._device.get("tempSel", 0.0)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._device.get("tempIndoor", 0.0)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        temp_setting = self._device.settings.get("settings.tempSel")
        if temp_setting is not None:
            temp_setting.value = str(int(temperature))
        self._attr_target_temperature = float(temperature)
        self.schedule_update_ha_state()
        self._schedule_debounce_settings()

    @property
    def hvac_mode(self) -> HVACMode:
        on_off = self._device.get("onOffStatus")
        mach = self._device.get("machMode")

        if on_off == 0:
            return HVACMode.OFF

        if mach not in HON_HVAC_MODE:
            return getattr(self, "_attr_hvac_mode", HVACMode.AUTO)

        mode = HON_HVAC_MODE[mach]

        if mode == HVACMode.AUTO and getattr(self, "_attr_hvac_mode", None) not in (
            None,
            HVACMode.OFF,
            HVACMode.AUTO,
        ):
            return self._attr_hvac_mode

        return mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._cancel_debounce()
        self._attr_target_temperature = None
        self._attr_fan_mode = None
        self._attr_swing_mode = None

        old_hvac_mode = getattr(self, "_attr_hvac_mode", None)
        old_onoff = self._device.settings.get("settings.onOffStatus")
        old_onoff_val = old_onoff.value if old_onoff else None
        old_mach = self._device.settings.get("settings.machMode")
        old_mach_val = old_mach.value if old_mach else None

        self._attr_hvac_mode = hvac_mode
        try:
            if hvac_mode == HVACMode.OFF:
                if old_onoff:
                    old_onoff.value = "0"
                await self._device.commands["settings"].send()
            else:
                if old_onoff:
                    old_onoff.value = "1"
                setting = self._device.settings.get("settings.machMode")
                if setting:
                    modes = {HON_HVAC_MODE[int(number)]: number for number in setting.values}
                    if hvac_mode in modes:
                        setting.value = modes[hvac_mode]
                    else:
                        await self.async_set_preset_mode(HON_HVAC_PROGRAM[hvac_mode])
                        return
                await self._device.commands["settings"].send()
        except Exception as exc:
            self._attr_hvac_mode = old_hvac_mode
            if old_onoff and old_onoff_val is not None:
                old_onoff.value = old_onoff_val
            if old_mach and old_mach_val is not None:
                old_mach.value = old_mach_val
            self._device.sync_params_to_command("settings")
            self.schedule_update_ha_state()
            raise HomeAssistantError(
                f"Nie udało się zmienić trybu pracy na {hvac_mode}: {exc}"
            ) from exc
        self.schedule_update_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._cancel_debounce()
        self._attr_target_temperature = None
        self._attr_fan_mode = None
        self._attr_swing_mode = None

        old_onoff = self._device.settings.get("settings.onOffStatus")
        old_onoff_val = old_onoff.value if old_onoff else None
        if old_onoff:
            old_onoff.value = "1"
        try:
            await self._device.commands["settings"].send()
        except Exception as exc:
            if old_onoff and old_onoff_val is not None:
                old_onoff.value = old_onoff_val
            self._device.sync_params_to_command("settings")
            self.schedule_update_ha_state()
            raise HomeAssistantError(
                f"Nie udało się włączyć klimatyzatora: {exc}"
            ) from exc
        self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    @property
    def preset_mode(self) -> str | None:
        """Return the current Preset for this channel."""
        return None

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the new preset mode."""
        self._cancel_debounce()
        self._attr_target_temperature = None
        self._attr_fan_mode = None
        self._attr_swing_mode = None

        old_preset = getattr(self, "_attr_preset_mode", None)
        prog_setting = self._device.settings.get("startProgram.program")
        old_prog_val = prog_setting.value if prog_setting else None
        onoff_setting = self._device.settings.get("settings.onOffStatus")
        old_onoff_val = onoff_setting.value if onoff_setting else None
        health_setting = self._device.settings.get("settings.healthMode")
        old_health_val = health_setting.value if health_setting else None

        if prog_setting:
            prog_setting.value = preset_mode
        self._device.sync_command("settings", "startProgram")
        is_uv = "uv" in preset_mode.lower()
        if "onOffStatus" in self._device.commands["startProgram"].parameters:
            with suppress(ValueError):
                self._device.commands["startProgram"].parameters["onOffStatus"].value = "1"
        if "healthMode" in self._device.commands["startProgram"].parameters:
            param = self._device.commands["startProgram"].parameters["healthMode"]
            if isinstance(param, HonParameterRange) and is_uv and param.max < 1:
                param.max = 1
            with suppress(ValueError):
                param.value = "1" if is_uv else "0"
        if onoff_setting:
            with suppress(ValueError):
                onoff_setting.value = "1"

        try:
            await self._device.commands["startProgram"].send()
        except Exception as exc:
            self._attr_preset_mode = old_preset
            if prog_setting and old_prog_val is not None:
                prog_setting.value = old_prog_val
            if onoff_setting and old_onoff_val is not None:
                onoff_setting.value = old_onoff_val
            if health_setting and old_health_val is not None:
                health_setting.value = old_health_val
            self._device.sync_params_to_command("settings")
            self.schedule_update_ha_state()
            raise HomeAssistantError(
                f"Nie udało się ustawić programu {preset_mode}: {exc}"
            ) from exc

        self._device.sync_command("startProgram", "settings")
        if onoff_setting:
            with suppress(ValueError):
                onoff_setting.value = "1"
        if health_setting:
            param = health_setting
            if isinstance(param, HonParameterRange) and is_uv and param.max < 1:
                param.max = 1
            with suppress(ValueError):
                param.value = "1" if is_uv else "0"
        self._set_temperature_bound()
        self._handle_coordinator_update(update=False)
        self.coordinator.async_set_updated_data({})
        self._attr_preset_mode = preset_mode
        self.schedule_update_ha_state()

    @property
    def fan_modes(self) -> list[str]:
        """Return the list of available fan modes."""
        fan_modes = []
        for mode in reversed(self._device.settings["settings.windSpeed"].values):
            fan_modes.append(HON_FAN[int(mode)])
        return fan_modes

    @property
    def fan_mode(self) -> str | None:
        """Return the fan setting."""
        if self._attr_fan_mode is not None:
            return self._attr_fan_mode
        raw = self._device.get("windSpeed")
        if raw is not None:
            with suppress(ValueError, TypeError, KeyError):
                if mode := HON_FAN.get(int(raw)):
                    return mode
        return None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        wind_setting = self._device.settings.get("settings.windSpeed")
        fan_modes: dict[str, str] = {}
        for mode in reversed(self._device.settings["settings.windSpeed"].values):
            fan_modes[HON_FAN[int(mode)]] = mode
        if wind_setting and fan_mode in fan_modes:
            wind_setting.value = str(fan_modes[fan_mode])
        self._attr_fan_mode = fan_mode
        self.schedule_update_ha_state()
        self._schedule_debounce_settings()

    @property
    def swing_mode(self) -> str | None:
        """Return the swing setting."""
        if self._attr_swing_mode is not None:
            return self._attr_swing_mode
        with suppress(ValueError, TypeError):
            horizontal = int(self._device.get("windDirectionHorizontal", 0))
            vertical = int(self._device.get("windDirectionVertical", 0))
            if horizontal == 7 and vertical == 8:
                return SWING_BOTH
            if horizontal == 7:
                return SWING_HORIZONTAL
            if vertical == 8:
                return SWING_VERTICAL
            return SWING_OFF
        return SWING_OFF

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        horizontal = self._device.settings.get("settings.windDirectionHorizontal")
        vertical = self._device.settings.get("settings.windDirectionVertical")

        if horizontal and vertical:
            if swing_mode in [SWING_BOTH, SWING_HORIZONTAL]:
                horizontal.value = "7"
            if swing_mode in [SWING_BOTH, SWING_VERTICAL]:
                vertical.value = "8"
            if swing_mode in [SWING_OFF, SWING_HORIZONTAL] and vertical.value == "8":
                vertical.value = "5"
            if swing_mode in [SWING_OFF, SWING_VERTICAL] and horizontal.value == "7":
                horizontal.value = "0"
        self._attr_swing_mode = swing_mode
        self.schedule_update_ha_state()
        self._schedule_debounce_settings()

    @callback
    def _handle_coordinator_update(self, update: bool = True) -> None:
        if update:
            self.schedule_update_ha_state()


class HonClimateEntity(HonEntity, ClimateEntity):
    entity_description: HonClimateEntityDescription
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: HonAppliance,
        description: HonClimateEntityDescription,
    ) -> None:
        super().__init__(hass, entry, device, description)

        self._attr_supported_features = (
            ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TARGET_TEMPERATURE
        )

        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._set_temperature_bound()

        self._attr_hvac_modes = [description.mode]
        if "stopProgram" in device.commands:
            self._attr_supported_features |= ClimateEntityFeature.TURN_OFF
            self._attr_hvac_modes += [HVACMode.OFF]
            modes: list[str] = []
        else:
            modes = ["no_mode"]

        for mode, data in device.commands["startProgram"].categories.items():
            if mode not in data.parameters["program"].values:
                continue
            if (zone := data.parameters.get("zone")) and isinstance(
                self.entity_description.name, str
            ):
                if self.entity_description.name.lower() in zone.values:
                    modes.append(mode)
            else:
                modes.append(mode)

        if modes:
            self._attr_supported_features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = modes

        self._handle_coordinator_update(update=False)

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._device.get(self.entity_description.key, 0.0)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        temp_key = self.entity_description.key.split(".")[-1].replace("Sel", "")
        return self._device.get(temp_key, 0.0)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        temp_setting = self._device.settings.get(self.entity_description.key)
        old_val = temp_setting.value if temp_setting is not None else None
        if temp_setting is not None:
            temp_setting.value = str(int(temperature))
        try:
            await self._device.commands["settings"].send()
        except Exception as exc:
            if temp_setting is not None and old_val is not None:
                temp_setting.value = old_val
            self._device.sync_params_to_command("settings")
            self.schedule_update_ha_state()
            raise HomeAssistantError(
                f"Nie udało się zmienić temperatury na {int(temperature)}°C: {exc}"
            ) from exc
        if temp_setting:
            with suppress(ValueError, TypeError):
                if "tempSel" in self._device.attributes.get("parameters", {}):
                    self._device.attributes["parameters"]["tempSel"].update(
                        str(int(temperature)), shield=True
                    )
        self.coordinator.async_set_updated_data({})
        self.schedule_update_ha_state()

    @property
    def hvac_mode(self) -> HVACMode:
        if self._device.get("onOffStatus") == 0:
            return HVACMode.OFF
        else:
            return self.entity_description.mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if len(self.hvac_modes) <= 1:
            return
        old_mode = getattr(self, "_attr_hvac_mode", None)
        self._attr_hvac_mode = hvac_mode
        command = "stopProgram" if hvac_mode == HVACMode.OFF else "startProgram"
        try:
            await self._device.commands[command].send()
        except Exception as exc:
            self._attr_hvac_mode = old_mode
            self.schedule_update_ha_state()
            raise HomeAssistantError(
                f"Nie udało się zmienić trybu na {hvac_mode}: {exc}"
            ) from exc
        self.schedule_update_ha_state()

    async def async_turn_on(self) -> None:
        """Set the HVAC State to on."""
        try:
            await self._device.commands["startProgram"].send()
        except Exception as exc:
            self.schedule_update_ha_state()
            raise HomeAssistantError(f"Nie udało się włączyć: {exc}") from exc

    async def async_turn_off(self) -> None:
        """Set the HVAC State to off."""
        try:
            await self._device.commands["stopProgram"].send()
        except Exception as exc:
            self.schedule_update_ha_state()
            raise HomeAssistantError(f"Nie udało się wyłączyć: {exc}") from exc

    @property
    def preset_mode(self) -> str | None:
        """Return the current Preset for this channel."""
        if self._device.get("onOffStatus") is not None:
            return self._device.get("programName", "")
        else:
            return self._device.get(
                f"mode{self.entity_description.key[-2:]}", "no_mode"
            )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the new preset mode."""
        old_preset = getattr(self, "_attr_preset_mode", None)
        if preset_mode == "no_mode" and HVACMode.OFF in self.hvac_modes:
            command = "stopProgram"
        elif preset_mode == "no_mode":
            command = "settings"
            self._device.commands["settings"].reset()
        else:
            command = "startProgram"
        program = self._device.settings.get(f"{command}.program")
        old_prog_val = program.value if program else None
        if program:
            program.value = preset_mode
        zone = self._device.settings.get(f"{command}.zone")
        if zone and isinstance(self.entity_description.name, str):
            zone.value = self.entity_description.name.lower()
        self._device.sync_command(command, "settings")
        self._set_temperature_bound()
        self._attr_preset_mode = preset_mode
        self.coordinator.async_set_updated_data({})
        try:
            await self._device.commands[command].send()
        except Exception as exc:
            self._attr_preset_mode = old_preset
            if program and old_prog_val is not None:
                program.value = old_prog_val
            self._device.sync_params_to_command("settings")
            self.schedule_update_ha_state()
            raise HomeAssistantError(
                f"Nie udało się ustawić trybu {preset_mode}: {exc}"
            ) from exc
        self.schedule_update_ha_state()

    def _set_temperature_bound(self) -> None:
        temperature = self._device.settings[self.entity_description.key]
        if not isinstance(temperature, HonParameterRange):
            raise ValueError
        self._attr_max_temp = temperature.max
        self._attr_target_temperature_step = temperature.step
        self._attr_min_temp = temperature.min

    @callback
    def _handle_coordinator_update(self, update: bool = True) -> None:
        if update:
            self.schedule_update_ha_state()
