"""Screen preview image for the IOTBT curtain."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SurplifeCoordinator
from .light import curtain_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the preview once the device is resolved as an IOTBT curtain."""
    coordinator: SurplifeCoordinator = entry.runtime_data.coordinator
    added = False

    @callback
    def _async_add_if_curtain() -> None:
        nonlocal added
        if not added and coordinator.is_curtain:
            added = True
            async_add_entities([SurplifeScreenPreview(hass, coordinator)])

    entry.async_on_unload(coordinator.async_add_listener(_async_add_if_curtain))
    _async_add_if_curtain()


class SurplifeScreenPreview(CoordinatorEntity[SurplifeCoordinator], ImageEntity):
    """A PNG of the first frame of the last uploaded animation.

    It is kept in memory, so it is empty after Home Assistant restarts until the next upload.
    """

    _attr_has_entity_name = True
    _attr_name = "Screen preview"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, coordinator: SurplifeCoordinator) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{coordinator.address}_screen_preview"
        self._attr_device_info = curtain_device_info(coordinator)
        self._attr_image_last_updated: datetime | None = coordinator.preview_updated

    async def async_image(self) -> bytes | None:
        return self.coordinator.preview_png

    @callback
    def _handle_coordinator_update(self) -> None:
        updated = self.coordinator.preview_updated
        if updated != self._attr_image_last_updated:
            self._attr_image_last_updated = updated
        self.async_write_ha_state()
