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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant
from pyhon.appliance import HonAppliance
from pyhon.parameter.range import HonParameterRange

from .const import HON_HVAC_MODE, HON_FAN, DOMAIN, HON_HVAC_PROGRAM
from .entity import HonEntity

_LOGGER = logging.getLogger(__name__)


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
        return self._device.get("tempSel", 0.0)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._device.get("tempIndoor", 0.0)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        self.debouncer.schedule_command("settings", {"tempSel": str(int(temperature))})

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
        self._attr_hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            self.debouncer.schedule_command("settings", {"onOffStatus": "0"})
        else:
            setting = self._device.settings.get("settings.machMode")
            if setting:
                modes = {
                    HON_HVAC_MODE[int(number)]: number for number in setting.values
                }
                if hvac_mode in modes:
                    self.debouncer.schedule_command(
                        "settings",
                        {"onOffStatus": "1", "machMode": str(modes[hvac_mode])},
                    )
                    self._set_temperature_bound()
                    return
                await self.async_set_preset_mode(HON_HVAC_PROGRAM[hvac_mode])
                return
            self.debouncer.schedule_command("settings", {"onOffStatus": "1"})

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.debouncer.schedule_command("settings", {"onOffStatus": "1"})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    @property
    def preset_mode(self) -> str | None:
        """Return the current Preset for this channel."""
        return None

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the new preset mode."""
        is_uv = "uv" in preset_mode.lower()
        self._attr_preset_mode = preset_mode
        self.debouncer.schedule_command(
            "startProgram",
            {
                "program": preset_mode,
                "onOffStatus": "1",
                "healthMode": "1" if is_uv else "0",
            },
        )
        self._set_temperature_bound()

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
        raw = self._device.get("windSpeed")
        if raw is not None:
            with suppress(ValueError, TypeError, KeyError):
                if mode := HON_FAN.get(int(raw)):
                    return mode
        return None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        fan_modes: dict[str, str] = {}
        for mode in reversed(self._device.settings["settings.windSpeed"].values):
            fan_modes[HON_FAN[int(mode)]] = mode
        if fan_mode in fan_modes:
            self.debouncer.schedule_command(
                "settings", {"windSpeed": str(fan_modes[fan_mode])}
            )

    @property
    def swing_mode(self) -> str | None:
        """Return the swing setting."""
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
        if not horizontal or not vertical:
            return
        h_val = str(self._device.get("windDirectionHorizontal", horizontal.value))
        v_val = str(self._device.get("windDirectionVertical", vertical.value))
        if swing_mode in [SWING_BOTH, SWING_HORIZONTAL]:
            h_val = "7"
        if swing_mode in [SWING_BOTH, SWING_VERTICAL]:
            v_val = "8"
        if swing_mode in [SWING_OFF, SWING_HORIZONTAL] and v_val == "8":
            v_val = "5"
        if swing_mode in [SWING_OFF, SWING_VERTICAL] and h_val == "7":
            h_val = "0"
        self.debouncer.schedule_command(
            "settings",
            {
                "windDirectionHorizontal": h_val,
                "windDirectionVertical": v_val,
            },
        )

    @callback
    def _handle_coordinator_update(self, update: bool = True) -> None:
        self._set_temperature_bound()
        super()._handle_coordinator_update(update=update)


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
        param_key = self.entity_description.key.split(".")[-1]
        self.debouncer.schedule_command("settings", {param_key: str(int(temperature))})

    @property
    def hvac_mode(self) -> HVACMode:
        if self._device.get("onOffStatus") == 0:
            return HVACMode.OFF
        else:
            return self.entity_description.mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if len(self.hvac_modes) <= 1:
            return
        self._attr_hvac_mode = hvac_mode
        command = "stopProgram" if hvac_mode == HVACMode.OFF else "startProgram"
        on_off = "0" if hvac_mode == HVACMode.OFF else "1"
        self.debouncer.schedule_command(command, {"onOffStatus": on_off})

    async def async_turn_on(self) -> None:
        """Set the HVAC State to on."""
        self.debouncer.schedule_command("startProgram", {"onOffStatus": "1"})

    async def async_turn_off(self) -> None:
        """Set the HVAC State to off."""
        self.debouncer.schedule_command("stopProgram", {"onOffStatus": "0"})

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
        if preset_mode == "no_mode" and HVACMode.OFF in self.hvac_modes:
            command = "stopProgram"
        elif preset_mode == "no_mode":
            command = "settings"
            self._device.commands["settings"].reset()
        else:
            command = "startProgram"
        changed: dict[str, Any] = {"program": preset_mode}
        zone = self._device.settings.get(f"{command}.zone")
        if zone and isinstance(self.entity_description.name, str):
            changed["zone"] = self.entity_description.name.lower()
        self._attr_preset_mode = preset_mode
        self.debouncer.schedule_command(command, changed)
        self._set_temperature_bound()

    def _set_temperature_bound(self) -> None:
        temperature = self._device.settings[self.entity_description.key]
        if not isinstance(temperature, HonParameterRange):
            raise ValueError
        self._attr_max_temp = temperature.max
        self._attr_target_temperature_step = temperature.step
        self._attr_min_temp = temperature.min

    @callback
    def _handle_coordinator_update(self, update: bool = True) -> None:
        super()._handle_coordinator_update(update=update)
