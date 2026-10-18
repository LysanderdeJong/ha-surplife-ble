"""Animation speed slider for the IOTBT curtain."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SurplifeCoordinator
from .light import curtain_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the speed slider once the device is resolved as an IOTBT curtain."""
    coordinator: SurplifeCoordinator = entry.runtime_data.coordinator
    added = False

    @callback
    def _async_add_if_curtain() -> None:
        nonlocal added
        if not added and coordinator.is_curtain:
            added = True
            async_add_entities([SurplifeAnimationSpeed(coordinator)])

    entry.async_on_unload(coordinator.async_add_listener(_async_add_if_curtain))
    _async_add_if_curtain()


class SurplifeAnimationSpeed(CoordinatorEntity[SurplifeCoordinator], NumberEntity):
    """Speed of the uploaded animation, 1 (slow) to 100 (very fast).

    It shows EA 81 byte 9 while the animation plays, and the last speed seen or set
    otherwise. Setting it plays the stored animation at that speed (``E2 09``).
    """

    _attr_has_entity_name = True
    _attr_name = "Animation speed"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: SurplifeCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_animation_speed"
        self._attr_device_info = curtain_device_info(coordinator)
        self._attr_native_value = float(coordinator.animation_speed)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_play_animation(speed=round(value))
        except (RuntimeError, ValueError, OSError, TimeoutError) as err:
            raise HomeAssistantError(str(err)) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_native_value = float(self.coordinator.animation_speed)
        self.async_write_ha_state()
