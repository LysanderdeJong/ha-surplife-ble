"""Screen media player for the IOTBT curtain: play images and GIFs on the 20x20 grid."""

from __future__ import annotations

from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import animation
from .coordinator import SurplifeCoordinator
from .core import curtain_c2
from .light import curtain_device_info

PLAY_MODE_HOLD = 2  # E2 09 mode 2 and up hold frame 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Screen entity once the device is resolved as an IOTBT curtain."""
    coordinator: SurplifeCoordinator = entry.runtime_data.coordinator
    added = False

    @callback
    def _async_add_if_curtain() -> None:
        nonlocal added
        if not added and coordinator.is_curtain:
            added = True
            async_add_entities([SurplifeScreen(coordinator)])

    entry.async_on_unload(coordinator.async_add_listener(_async_add_if_curtain))
    _async_add_if_curtain()


class SurplifeScreen(CoordinatorEntity[SurplifeCoordinator], MediaPlayerEntity):
    """The curtain as a screen. Images and GIFs are uploaded to its flash."""

    _attr_has_entity_name = True
    _attr_name = "Screen"
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.BROWSE_MEDIA
    )

    def __init__(self, coordinator: SurplifeCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_screen"
        self._attr_device_info = curtain_device_info(coordinator)
        self._async_update_attrs()

    @callback
    def _async_update_attrs(self) -> None:
        state = self.coordinator.data
        if state is None:
            return
        if state.power is False:
            self._attr_state = MediaPlayerState.OFF
        elif state.mode == curtain_c2.MODE_DIY:
            held = state.play_mode is not None and state.play_mode >= PLAY_MODE_HOLD
            self._attr_state = MediaPlayerState.PAUSED if held else MediaPlayerState.PLAYING
        else:
            self._attr_state = MediaPlayerState.ON
        self._attr_media_title = self.coordinator.animation_title

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_power(False)

    async def async_media_stop(self) -> None:
        await self.coordinator.async_set_power(False)

    async def async_media_play(self) -> None:
        """Loop the stored animation (E2 09 mode 1)."""
        await self._async_play(curtain_c2.DEFAULT_PLAY_MODE)

    async def async_media_pause(self) -> None:
        """Hold frame 1 of the stored animation (E2 09 mode 2)."""
        await self._async_play(PLAY_MODE_HOLD)

    async def _async_play(self, mode: int) -> None:
        try:
            await self.coordinator.async_play_animation(mode)
        except (RuntimeError, ValueError, OSError, TimeoutError) as err:
            raise HomeAssistantError(str(err)) from err

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Upload an image or GIF (media source, http(s) URL or allowed path) and play it."""
        if not animation.is_supported_media_type(media_type):
            raise HomeAssistantError(
                f"unsupported media type {media_type!r}: use an image/* type or url"
            )
        source = await animation.async_resolve_media(self.hass, media_id, self.entity_id)
        await animation.async_upload(
            self.hass,
            self.coordinator,
            source,
            mode=curtain_c2.DEFAULT_PLAY_MODE,
            speed=self.coordinator.animation_speed,
            brightness=self.coordinator.curtain_percent(),
        )

    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse media sources, showing only images."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("image/"),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        self.async_write_ha_state()
