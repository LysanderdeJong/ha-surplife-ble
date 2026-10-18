"""Minimal fake Home Assistant modules so the integration can be tested without HA."""

from __future__ import annotations

import enum
import importlib
import logging
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol

_INTEGRATION = "custom_components.surplife_ble"
_MODULES = (
    "coordinator",
    "light",
    "media_player",
    "image",
    "number",
    "animation",
    "services",
    "config_flow",
)


class HomeAssistantError(Exception):
    pass


class FakeEntity:
    """Base for fake entities: records state writes."""

    hass: Any = None
    entity_id = "light.test"

    def __init__(self) -> None:
        self.writes = 0

    def async_write_ha_state(self) -> None:
        self.writes += 1


def _module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class FakeSession:
    """Fake aiohttp session: ``responses`` maps a URL to (status, headers, chunks)."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, dict[str, str], list[bytes]]] = {}
        self.requested: list[str] = []

    def get(self, url: str) -> Any:
        self.requested.append(url)
        status, headers, chunks = self.responses.get(url, (404, {}, []))

        class _Content:
            async def iter_chunked(self, _size: int) -> Any:
                for chunk in chunks:
                    yield chunk

        class _Response:
            def __init__(self) -> None:
                self.status = status
                self.headers = headers
                self.content = _Content()

            async def __aenter__(self) -> _Response:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

        return _Response()


class FakeConfig:
    def __init__(self, allowed: tuple[str, ...] = ()) -> None:
        self.allowed = allowed

    def is_allowed_path(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.allowed)


class FakeHass:
    def __init__(self, allowed: tuple[str, ...] = ()) -> None:
        self.config = FakeConfig(allowed)
        self.tasks: list[Any] = []

    def async_create_task(self, coroutine: Any) -> None:
        coroutine.close()

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


class FakeEntry:
    title = "Test curtain"
    data = {"address": "DEVICE"}

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.unload: list[Any] = []
        self.runtime_data: Any = None
        self.options: dict[str, Any] = options or {}
        self.update_listeners: list[Any] = []

    def add_update_listener(self, listener: Any) -> Any:
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)

    def async_on_unload(self, func: Any) -> None:
        self.unload.append(func)


class FakePlatform:
    def __init__(self) -> None:
        self.services: dict[str, tuple[Any, str]] = {}

    def async_register_entity_service(self, name: str, schema: Any, method: str) -> None:
        self.services[name] = (schema, method)


def install(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install fake ``homeassistant`` modules and import the integration modules."""

    class ColorMode(enum.StrEnum):
        RGB = "rgb"
        WHITE = "white"

    class LightEntityFeature(enum.IntFlag):
        EFFECT = 4

    class MediaPlayerEntityFeature(enum.IntFlag):
        PAUSE = 1
        TURN_ON = 128
        TURN_OFF = 256
        PLAY_MEDIA = 512
        BROWSE_MEDIA = 131072
        STOP = 4096
        PLAY = 16384

    class MediaPlayerState(enum.StrEnum):
        OFF = "off"
        ON = "on"
        IDLE = "idle"
        PLAYING = "playing"
        PAUSED = "paused"

    class NumberMode(enum.StrEnum):
        SLIDER = "slider"

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item: object) -> type[DataUpdateCoordinator]:
            return cls

        def __init__(self, hass: Any, _logger: logging.Logger, *, config_entry: Any, **_: Any):
            self.hass = hass
            self.config_entry = config_entry
            self.data = None
            self.listeners: list[Any] = []

        async def async_request_refresh(self) -> None:
            return None

        def async_set_updated_data(self, data: Any) -> None:
            self.data = data
            self.async_update_listeners()

        def async_add_listener(self, update_callback: Any) -> Any:
            self.listeners.append(update_callback)
            return lambda: self.listeners.remove(update_callback)

        def async_update_listeners(self) -> None:
            for listener in list(self.listeners):
                listener()

    class UpdateFailed(Exception):
        pass

    class CoordinatorEntity(FakeEntity):
        @classmethod
        def __class_getitem__(cls, _item: object) -> type[CoordinatorEntity]:
            return cls

        def __init__(self, coordinator: Any) -> None:
            super().__init__()
            self.coordinator = coordinator
            self.hass = coordinator.hass

    class LightEntity:
        _attr_brightness: int | None = None

        @property
        def brightness(self) -> int | None:
            return self._attr_brightness

    class MediaPlayerEntity:
        pass

    class NumberEntity:
        pass

    class ImageEntity:
        def __init__(self, hass: Any) -> None:
            self.hass = hass

    class BrowseMedia:
        pass

    class ConfigFlow:
        def __init_subclass__(cls, *, domain: str | None = None, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            cls.domain = domain

    class OptionsFlow:
        def async_show_form(self, *, step_id: str, data_schema: Any, errors: Any = None) -> Any:
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
            }

        def async_create_entry(self, *, data: Any, title: str = "") -> Any:
            return {"type": "create_entry", "data": data, "title": title}

        def async_abort(self, *, reason: str) -> Any:
            return {"type": "abort", "reason": reason}

    class NumberSelectorMode(enum.StrEnum):
        SLIDER = "slider"

    class SelectSelectorMode(enum.StrEnum):
        DROPDOWN = "dropdown"

    class SelectSelector:
        """Like the real selector, it only accepts one of the options."""

        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config

        def __call__(self, value: Any) -> str:
            if value not in self.config["options"]:
                raise vol.Invalid(f"{value!r} is not one of the options")
            return str(value)

    class BooleanSelector:
        def __call__(self, value: Any) -> bool:
            if not isinstance(value, bool):
                raise vol.Invalid("expected a boolean")
            return value

    class NumberSelector:
        """Like the real selector, it rejects values outside min/max."""

        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config

        def __call__(self, value: Any) -> float:
            number = float(value)
            if not self.config["min"] <= number <= self.config["max"]:
                raise vol.Invalid(f"{number} is outside the allowed range")
            return number

    class PlayMedia:
        def __init__(self, url: str, mime_type: str, path: Any = None) -> None:
            self.url = url
            self.mime_type = mime_type
            self.path = path

    session = FakeSession()
    media = SimpleNamespace(resolved={}, browse_calls=[], resolve_calls=[])

    def is_media_source_id(media_id: str) -> bool:
        return media_id.startswith("media-source://")

    async def async_resolve_media(hass: Any, media_id: str, entity_id: str | None) -> PlayMedia:
        media.resolve_calls.append((media_id, entity_id))
        return media.resolved[media_id]

    async def async_browse_media(hass: Any, media_id: str | None, *, content_filter: Any) -> str:
        media.browse_calls.append((media_id, content_filter))
        return "browse-result"

    def boolean(value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on", "enable")
        return bool(value)

    components = _module("homeassistant.components")
    modules = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": components,
        "homeassistant.components.bluetooth": _module(
            "homeassistant.components.bluetooth",
            async_ble_device_from_address=lambda *_a, **_k: None,
            async_discovered_service_info=lambda *_a, **_k: [],
            BluetoothServiceInfo=object,
            BluetoothServiceInfoBleak=object,
        ),
        "homeassistant.components.light": _module(
            "homeassistant.components.light",
            ATTR_BRIGHTNESS="brightness",
            ATTR_EFFECT="effect",
            ATTR_RGB_COLOR="rgb_color",
            ATTR_WHITE="white",
            ColorMode=ColorMode,
            LightEntity=LightEntity,
            LightEntityFeature=LightEntityFeature,
        ),
        "homeassistant.components.media_player": _module(
            "homeassistant.components.media_player",
            BrowseMedia=BrowseMedia,
            MediaPlayerEntity=MediaPlayerEntity,
            MediaPlayerEntityFeature=MediaPlayerEntityFeature,
            MediaPlayerState=MediaPlayerState,
        ),
        "homeassistant.components.media_source": _module(
            "homeassistant.components.media_source",
            is_media_source_id=is_media_source_id,
            async_resolve_media=async_resolve_media,
            async_browse_media=async_browse_media,
            PlayMedia=PlayMedia,
        ),
        "homeassistant.components.image": _module(
            "homeassistant.components.image", ImageEntity=ImageEntity
        ),
        "homeassistant.components.number": _module(
            "homeassistant.components.number", NumberEntity=NumberEntity, NumberMode=NumberMode
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries",
            ConfigEntry=object,
            ConfigFlow=ConfigFlow,
            ConfigFlowResult=dict,
            OptionsFlow=OptionsFlow,
        ),
        "homeassistant.helpers.selector": _module(
            "homeassistant.helpers.selector",
            BooleanSelector=BooleanSelector,
            NumberSelector=NumberSelector,
            NumberSelectorConfig=lambda **kwargs: kwargs,
            NumberSelectorMode=NumberSelectorMode,
            SelectSelector=SelectSelector,
            SelectSelectorConfig=lambda **kwargs: kwargs,
            SelectSelectorMode=SelectSelectorMode,
        ),
        "homeassistant.const": _module(
            "homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name"
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, callback=lambda func: func
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=HomeAssistantError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry",
            CONNECTION_BLUETOOTH="bluetooth",
            DeviceInfo=dict,
        ),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform",
            AddConfigEntryEntitiesCallback=object,
            async_get_current_platform=lambda: modules_platform[0],
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=DataUpdateCoordinator,
            UpdateFailed=UpdateFailed,
            CoordinatorEntity=CoordinatorEntity,
        ),
        "homeassistant.helpers.aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda _hass: session
        ),
        "homeassistant.helpers.network": _module(
            "homeassistant.helpers.network", get_url=lambda _hass, **_k: "http://ha.local:8123"
        ),
        "homeassistant.helpers.config_validation": _module(
            "homeassistant.helpers.config_validation", string=str, boolean=boolean
        ),
    }
    modules_platform: list[FakePlatform] = [FakePlatform()]
    helpers = modules["homeassistant.helpers"]
    components.bluetooth = modules["homeassistant.components.bluetooth"]  # type: ignore[attr-defined]
    components.media_source = modules["homeassistant.components.media_source"]  # type: ignore[attr-defined]
    helpers.device_registry = modules["homeassistant.helpers.device_registry"]  # type: ignore[attr-defined]
    helpers.entity_platform = modules["homeassistant.helpers.entity_platform"]  # type: ignore[attr-defined]
    helpers.config_validation = modules["homeassistant.helpers.config_validation"]  # type: ignore[attr-defined]
    helpers.selector = modules["homeassistant.helpers.selector"]  # type: ignore[attr-defined]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    package = importlib.import_module(_INTEGRATION)
    for name in _MODULES:
        monkeypatch.delitem(sys.modules, f"{_INTEGRATION}.{name}", raising=False)
        # "from . import name" prefers a stale attribute on the package over sys.modules.
        monkeypatch.delattr(package, name, raising=False)
    loaded = {name: importlib.import_module(f"{_INTEGRATION}.{name}") for name in _MODULES}
    return SimpleNamespace(
        session=session,
        media=media,
        platform=modules_platform[0],
        PlayMedia=PlayMedia,
        ColorMode=ColorMode,
        LightEntityFeature=LightEntityFeature,
        MediaPlayerEntityFeature=MediaPlayerEntityFeature,
        MediaPlayerState=MediaPlayerState,
        NumberMode=NumberMode,
        **loaded,
    )


def make_coordinator(
    ha: SimpleNamespace,
    fake_device: Any,
    hass: FakeHass | None = None,
    options: dict[str, Any] | None = None,
) -> Any:
    """A real coordinator wired to a real LightClient over ``fake_device``."""
    from curtain_fakes import _Device

    from custom_components.surplife_ble.core.client import LightClient

    coordinator = ha.coordinator.SurplifeCoordinator(hass or FakeHass(), FakeEntry(options))
    device = _Device()
    coordinator._ble_device = device
    coordinator.client = LightClient(
        device,  # type: ignore[arg-type]
        client_factory=lambda _device: fake_device,
    )
    return coordinator

