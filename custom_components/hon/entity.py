import asyncio
from contextlib import suppress
import logging
from typing import Optional, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from pyhon.appliance import HonAppliance
from pyhon.parameter.range import HonParameterRange

from .const import DOMAIN
from .typedefs import HonEntityDescription

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_DELAY = 2.0


class HonDeviceDebouncer:
    """Centralized debouncer accumulating all parameter changes per device."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: HonAppliance,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
    ) -> None:
        self._hass = hass
        self._device = device
        self._coordinator = coordinator
        self._timer_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._has_pending_changes = False
        self._pending_command: str | None = None
        self._dirty_params: dict[str, Any] = {}
        self._snapshot: dict[str, Any] = {}
        self._snapshot_attrs: dict[str, Any] = {}

    @property
    def has_pending_changes(self) -> bool:
        return self._has_pending_changes

    def get_pending(self, key: str) -> Any:
        return self._dirty_params.get(key)

    def cancel(self) -> None:
        """Cancel any pending debounce timer."""
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None

    def _apply_param_to_setting(self, setting: Any, val: Any) -> None:
        if isinstance(setting, HonParameterRange):
            with suppress(ValueError, TypeError):
                f_val = float(val)
                if f_val < setting.min:
                    setting.min = f_val
                if f_val > setting.max:
                    setting.max = f_val
                setting.value = f_val
                return
        with suppress(ValueError, TypeError):
            setting.value = str(val)

    def _apply_dirty_params(self, dirty: dict[str, Any], target_cmd: str) -> None:
        """Apply all accumulated dirty parameters to settings, commands, and attributes."""
        if "program" in dirty:
            prog_val = str(dirty["program"])
            if prog_setting := self._device.settings.get("startProgram.program"):
                with suppress(ValueError, TypeError):
                    prog_setting.value = prog_val
            self._device.sync_command("settings", "startProgram")
            if "startProgram" in self._device.commands and "machMode" not in dirty:
                if sp_mach := self._device.commands["startProgram"].parameters.get(
                    "machMode"
                ):
                    if s_mach := self._device.settings.get("settings.machMode"):
                        self._apply_param_to_setting(s_mach, sp_mach.value)
                        if "machMode" in self._device.attributes.get("parameters", {}):
                            self._device.attributes["parameters"]["machMode"].update(
                                str(sp_mach.value), shield=True
                            )

            if self._device.appliance_type == "AC":
                prog_lower = prog_val.lower()
                s_mach = self._device.settings.get("settings.machMode")
                mach_values = (
                    s_mach.values if s_mach and hasattr(s_mach, "values") else []
                )
                ac_mach = None
                if "fan" in prog_lower:
                    ac_mach = "5" if "5" in mach_values else ("6" if "6" in mach_values else "5")
                    dirty.pop("tempSel", None)
                elif "cool" in prog_lower:
                    ac_mach = "1"
                elif "heat" in prog_lower:
                    ac_mach = "4"
                elif "dry" in prog_lower:
                    ac_mach = "3" if "3" in mach_values else ("2" if "2" in mach_values else "3")
                elif "auto" in prog_lower:
                    ac_mach = "0"

                if ac_mach is not None:
                    dirty["machMode"] = ac_mach
                    if s_mach:
                        self._apply_param_to_setting(s_mach, ac_mach)
                    if "machMode" in self._device.attributes.get("parameters", {}):
                        self._device.attributes["parameters"]["machMode"].update(
                            str(ac_mach), shield=True
                        )

        for base_key in ("onOffStatus", "machMode"):
            if base_key in dirty:
                if setting := self._device.settings.get(f"settings.{base_key}"):
                    self._apply_param_to_setting(setting, dirty[base_key])

        for k, v in dirty.items():
            if k in ("program", "onOffStatus", "machMode") or isinstance(v, bool):
                continue
            if setting := self._device.settings.get(f"settings.{k}"):
                self._apply_param_to_setting(setting, v)

        if "startProgram" in self._device.commands:
            if target_cmd == "startProgram":
                self._device.sync_command("settings", "startProgram")
            sp_params = self._device.commands["startProgram"].parameters
            for k, v in dirty.items():
                if k == "program" or isinstance(v, bool):
                    continue
                if k in sp_params:
                    self._apply_param_to_setting(sp_params[k], v)
        elif target_cmd != "settings" and target_cmd in self._device.commands:
            self._device.sync_command("settings", target_cmd)
            cmd_params = self._device.commands[target_cmd].parameters
            for k, v in dirty.items():
                if isinstance(v, bool):
                    continue
                if k in cmd_params:
                    self._apply_param_to_setting(cmd_params[k], v)

        params_dict = self._device.attributes.get("parameters", {})
        for k, v in dirty.items():
            if isinstance(v, bool):
                self._device.attributes[k] = v
            elif k in params_dict:
                with suppress(ValueError, TypeError):
                    if isinstance(v, float) and v.is_integer():
                        params_dict[k].update(str(int(v)), shield=True)
                    else:
                        params_dict[k].update(str(v), shield=True)

        if "program" in dirty:
            self._device.attributes["programName"] = str(dirty["program"])
            if self._device.appliance_type == "AC":
                is_uv = "uv" in str(dirty["program"]).lower()
                h_val = "1" if is_uv else "0"
                dirty["healthMode"] = h_val
                if h_setting := self._device.settings.get("settings.healthMode"):
                    self._apply_param_to_setting(h_setting, h_val)
                if "startProgram" in self._device.commands:
                    if h_param := self._device.commands["startProgram"].parameters.get("healthMode"):
                        self._apply_param_to_setting(h_param, h_val)
                if "healthMode" in params_dict:
                    params_dict["healthMode"].update(h_val, shield=True)

        if self._device._extra:
            saved_prog = (
                self._device.attributes.get("programName")
                if "program" in dirty
                else None
            )
            self._device._attributes = self._device._extra.attributes(
                self._device._attributes
            )
            if saved_prog:
                self._device.attributes["programName"] = saved_prog

    def reapply_pending(self) -> None:
        """Re-apply pending dirty parameters if an external update arrived during debounce."""
        if self._has_pending_changes and self._dirty_params:
            self._apply_dirty_params(
                self._dirty_params, self._pending_command or "settings"
            )

    def schedule_command(
        self,
        command: str = "settings",
        changed_params: dict[str, Any] | None = None,
    ) -> None:
        """Accumulate parameter changes and schedule API send after DEBOUNCE_DELAY."""
        if not self._has_pending_changes:
            self._snapshot = {
                k: attr.value
                for k, attr in self._device.attributes.get("parameters", {}).items()
            }
            self._snapshot_attrs = {
                k: self._device.attributes.get(k)
                for k in ("active", "pause", "programName")
                if k in self._device.attributes
            }
            if "settings.machMode" in self._device.settings:
                current_mach = self._device.get("machMode")
                if current_mach is not None:
                    with suppress(ValueError, TypeError):
                        mach_str = str(int(current_mach))
                        if (
                            str(self._device.settings["settings.machMode"].value)
                            != mach_str
                        ):
                            self._device.settings["settings.machMode"].value = mach_str
            if "settings.onOffStatus" in self._device.settings:
                current_onoff = self._device.get("onOffStatus")
                if current_onoff is not None:
                    with suppress(ValueError, TypeError):
                        onoff_str = str(int(current_onoff))
                        if (
                            str(self._device.settings["settings.onOffStatus"].value)
                            != onoff_str
                        ):
                            self._device.settings["settings.onOffStatus"].value = (
                                onoff_str
                            )
            self._has_pending_changes = True

        if changed_params:
            for k, v in changed_params.items():
                self._dirty_params.pop(k, None)
                self._dirty_params[k] = v

        if command == "startProgram":
            self._pending_command = "startProgram"
        elif command == "stopProgram":
            self._pending_command = "stopProgram"
        elif command == "settings":
            if self._pending_command == "startProgram":
                if changed_params and (
                    str(changed_params.get("onOffStatus")) == "0"
                    or "machMode" in changed_params
                ):
                    self._pending_command = "settings"
                    self._dirty_params.pop("program", None)
            else:
                self._pending_command = "settings"
        else:
            self._pending_command = command

        self._apply_dirty_params(
            self._dirty_params, self._pending_command or "settings"
        )
        self._coordinator.async_set_updated_data({})

        self.cancel()
        self._timer_task = self._hass.async_create_task(self._async_timer_expired())

    async def _async_timer_expired(self) -> None:
        """Wait for debounce period of inactivity before sending."""
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
        except asyncio.CancelledError:
            return
        await self._async_execute_send()

    async def _async_execute_send(self) -> None:
        """Send accumulated command to the API."""
        if not self._has_pending_changes:
            return

        command_to_send = self._pending_command or "settings"
        dirty_to_send = dict(self._dirty_params)
        snapshot_to_restore = dict(self._snapshot)
        snapshot_attrs_to_restore = dict(self._snapshot_attrs)

        self._has_pending_changes = False
        self._pending_command = None
        self._dirty_params.clear()
        self._snapshot.clear()
        self._snapshot_attrs.clear()

        async with self._send_lock:
            self._apply_dirty_params(dirty_to_send, command_to_send)

            try:
                await self._device.commands[command_to_send].send()
            except Exception as exc:
                _LOGGER.error(
                    "Nie udało się wysłać skumulowanych ustawień (%s): %s",
                    command_to_send,
                    exc,
                )
                if not self._has_pending_changes:
                    params_dict = self._device.attributes.get("parameters", {})
                    for k, v in snapshot_to_restore.items():
                        if k in params_dict:
                            params_dict[k].update(str(v), shield=True)
                    for k, v in snapshot_attrs_to_restore.items():
                        self._device.attributes[k] = v
                    if self._device._extra:
                        self._device._attributes = self._device._extra.attributes(
                            self._device._attributes
                        )
                    self._device.sync_params_to_command("settings")
                    self._device.sync_params_to_command("startProgram")
                    self._coordinator.async_set_updated_data({})
                return

            if command_to_send != "settings":
                self._device.sync_command(command_to_send, "settings")
            self._apply_dirty_params(dirty_to_send, "settings")

            if not self._has_pending_changes:
                self._coordinator.async_set_updated_data({})


class HonEntity(CoordinatorEntity[DataUpdateCoordinator[dict[str, Any]]]):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: HonAppliance,
        description: Optional[HonEntityDescription] = None,
    ) -> None:
        entry_data = hass.data[DOMAIN][entry.unique_id]
        self.coordinator = entry_data["coordinator"]
        super().__init__(self.coordinator)
        self._hon = entry_data["hon"]
        self._hass = hass
        self._device: HonAppliance = device

        debouncers: dict[str, HonDeviceDebouncer] = entry_data.setdefault(
            "debouncers", {}
        )
        if device.unique_id not in debouncers:
            debouncers[device.unique_id] = HonDeviceDebouncer(
                hass, device, self.coordinator
            )
        self.debouncer: HonDeviceDebouncer = debouncers[device.unique_id]

        if description is not None:
            self.entity_description = description
            self._attr_unique_id = f"{self._device.unique_id}{description.key}"
        else:
            self._attr_unique_id = self._device.unique_id
        self._handle_coordinator_update(update=False)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.unique_id)},
            manufacturer=self._device.get("brand", "").capitalize(),
            name=self._device.nick_name,
            model=self._device.model_name,
            sw_version=self._device.get("fwVersion", ""),
            hw_version=f"{self._device.appliance_type}{self._device.model_id}",
            serial_number=self._device.get("serialNumber", ""),
        )

    @callback
    def _handle_coordinator_update(self, update: bool = True) -> None:
        if self.debouncer.has_pending_changes:
            self.debouncer.reapply_pending()
        if update:
            self.schedule_update_ha_state()
