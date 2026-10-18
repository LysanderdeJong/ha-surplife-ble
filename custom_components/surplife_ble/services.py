"""Entity services for the IOTBT curtain: upload_animation and play_animation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from .core import curtain_c2

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import EntityPlatform

SERVICE_UPLOAD_ANIMATION = "upload_animation"
SERVICE_PLAY_ANIMATION = "play_animation"

ATTR_PATH = "path"
ATTR_URL = "url"
ATTR_SPEED = "speed"
ATTR_BRIGHTNESS = "brightness"
ATTR_MODE = "mode"
ATTR_CONTROLLER_SIDE = "controller_side"
ATTR_FLIP = "flip"
ATTR_MAX_FRAMES = "max_frames"
ATTR_RESAMPLE = "resample"

_PERCENT = vol.All(vol.Coerce(int), vol.Range(min=1, max=100))
_MODE = vol.All(vol.Coerce(int), vol.Range(min=0, max=255))

PLAY_SCHEMA = {
    vol.Optional(ATTR_MODE, default=curtain_c2.DEFAULT_PLAY_MODE): _MODE,
    vol.Optional(ATTR_SPEED, default=50): _PERCENT,
    vol.Optional(ATTR_BRIGHTNESS, default=100): _PERCENT,
}

UPLOAD_SCHEMA = {
    vol.Optional(ATTR_PATH): cv.string,
    vol.Optional(ATTR_URL): cv.string,
    **PLAY_SCHEMA,
    vol.Optional(ATTR_CONTROLLER_SIDE, default="left"): vol.In(["left", "right"]),
    vol.Optional(ATTR_FLIP, default=False): cv.boolean,
    vol.Optional(ATTR_MAX_FRAMES, default=curtain_c2.MAX_UPLOAD_FRAMES): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=curtain_c2.MAX_UPLOAD_FRAMES)
    ),
    vol.Optional(ATTR_RESAMPLE, default="box"): vol.In(["nearest", "box", "lanczos"]),
}


def async_register_entity_services(platform: EntityPlatform) -> None:
    """Register the curtain services on the light platform."""
    platform.async_register_entity_service(
        SERVICE_UPLOAD_ANIMATION, UPLOAD_SCHEMA, "async_upload_animation"
    )
    platform.async_register_entity_service(
        SERVICE_PLAY_ANIMATION, PLAY_SCHEMA, "async_play_animation"
    )
