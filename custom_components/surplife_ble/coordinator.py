"""Bluetooth connection and state coordination for Surplife BLE."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CALIBRATION_OPTIONS, CONF_CONTROLLER_SIDE, CONF_FLIP, UPDATE_INTERVAL
from .core import curtain_c2
from .core.calibration import IDENTITY, CalibrationError, ColorCalibration
from .core.client import LightClient
from .core.models import LightState, apply_assumed_rgb, rgb_state_matches

_LOGGER = logging.getLogger(__name__)
_ASSUMED_RGB_SECONDS = 5.0
_CONFIRMATION_DELAY_SECONDS = 0.3
# Upper bound for the GATT work of one poll or command once connected. The
# connection itself is bounded by bleak_retry_connector, which needs longer on
# Bluetooth proxies, so it is not included.
_OPERATION_TIMEOUT_SECONDS = 15.0
# Identifying an unresolved device writes up to 12 probe frames to FF01 and
# waits 1.25 s after each (twice when a reply needs confirming), so it needs
# a larger budget than a resolved query.
_PROBE_TIMEOUT_SECONDS = 60.0
# A full 128-frame curtain upload takes about 30 s on a good link.
_UPLOAD_TIMEOUT_SECONDS = 180.0
_DISCONNECT_TIMEOUT_SECONDS = 10.0
# Upper bound for one connection attempt. bleak_retry_connector retries a
# cancelled connection (ESP_GATT_CONN_CONN_CANCEL) up to nine times with a
# backoff, which takes minutes for a lamp at the edge of range. A connection
# that works, even through a proxy, completes well within this.
_CONNECT_ATTEMPT_TIMEOUT_SECONDS = 15.0
# Connection attempts a command makes before giving up. The Bluetooth route is
# refreshed before each one. Polls make one attempt each, so they retry every
# poll interval on their own.
_COMMAND_CONNECT_ATTEMPTS = 2
# After a command runs out of connection attempts, commands fail at once for
# this long instead of each making its own attempts behind the device lock.
# Polls keep trying.
_CONNECT_COOLDOWN_SECONDS = 30.0
# A link can stay "connected" while the lamp stops answering. Drop it after this
# many failed polls in a row, so the next poll makes a fresh connection.
_RECONNECT_AFTER_FAILURES = 2
# Keep showing the last known state (marked assumed) for this many failed polls
# in a row before the entity becomes unavailable.
_TOLERATED_FAILURES = 3
# Wait between attempts to identify an unresolved device, doubling up to the cap.
_PROBE_BACKOFF_BASE_SECONDS = 15.0
_PROBE_BACKOFF_MAX_SECONDS = 300.0


class ConnectionFailed(UpdateFailed):
    """Connecting to the device failed or took too long."""


@dataclass(slots=True)
class SurplifeRuntimeData:
    """Runtime data stored on the config entry."""

    coordinator: SurplifeCoordinator


class SurplifeCoordinator(DataUpdateCoordinator[LightState]):
    """Manage the resolved client and expose normalized state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=UPDATE_INTERVAL,
            # BLE devices can't handle concurrent commands — serialize per device
            always_update=False,
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.client: LightClient | None = None
        self._ble_device: BLEDevice | None = None
        self._advertisement: AdvertisementData | None = None
        self._was_available: bool = False
        self._operation_lock = asyncio.Lock()
        self._assumed_rgb: tuple[int, int, int] | None = None
        self._assumed_rgb_brightness: int | None = None
        self._assumed_rgb_deadline: float | None = None
        self._confirmation_generation = 0
        self._consecutive_failures = 0
        self._probe_failures = 0
        self._next_probe_at: float | None = None
        self._connect_failed_at: float | None = None
        self._calibration = self._load_calibration()
        self._controller_side, self._flip = self._load_orientation()
        # IOTBT curtain only: kept in memory for the Screen entities.
        self.animation_title: str | None = None
        self.preview_png: bytes | None = None
        self.preview_updated: datetime | None = None
        self._last_animation_speed: int = 50

    @property
    def calibration(self) -> ColorCalibration:
        """The curtain color calibration from the entry options (identity if unset)."""
        return self._calibration

    def _load_calibration(self) -> ColorCalibration:
        options = {
            key: value
            for key, value in dict(self.config_entry.options).items()
            if key in CALIBRATION_OPTIONS
        }
        try:
            return ColorCalibration.from_dict(options)
        except CalibrationError as err:
            _LOGGER.warning("Ignoring invalid color calibration options: %s", err)
            return IDENTITY

    @property
    def controller_side(self) -> str:
        """Which side of the picture the controller box hangs on: ``left`` or ``right``."""
        return self._controller_side

    @property
    def flip(self) -> bool:
        """True when the curtain hangs upside down (swap top and bottom)."""
        return self._flip

    def _load_orientation(self) -> tuple[str, bool]:
        options = self.config_entry.options
        side = options.get(CONF_CONTROLLER_SIDE, "left")
        if side not in curtain_c2.CONTROLLER_SIDES:
            _LOGGER.warning("Ignoring invalid controller side option %r", side)
            side = "left"
        return side, bool(options.get(CONF_FLIP, False))

    def async_update_options(self) -> None:
        """Apply changed options (calibration and orientation) without reconnecting."""
        self._calibration = self._load_calibration()
        self._controller_side, self._flip = self._load_orientation()
        if self.client is not None:
            self.client.curtain_calibration = self._calibration

    def async_set_ble_device(
        self, device: BLEDevice, advertisement: AdvertisementData | None
    ) -> None:
        """Refresh the nearest-adapter BLEDevice from Home Assistant's cache."""
        self._ble_device = device
        self._advertisement = advertisement
        if self.client:
            self.client.set_ble_device(device, advertisement)

    def _current_ble_device(self, fallback: BLEDevice | None = None) -> BLEDevice | None:
        return (
            bluetooth.async_ble_device_from_address(
                self.hass,
                self.address.upper(),
                True,
            )
            or self._ble_device
            or fallback
        )

    async def _async_client_factory(self, device: BLEDevice) -> BleakClientWithServiceCache:
        """Use Home Assistant's retrying connection helper and service cache."""
        return await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.config_entry.title,
            lambda disconnected: (
                self.client.mark_disconnected(disconnected) if self.client else None
            ),
            use_services_cache=True,
            ble_device_callback=lambda: self._current_ble_device(device) or device,
        )

    async def _async_get_client(self, *, attempts: int = 1) -> LightClient:
        loop = asyncio.get_running_loop()
        for attempt in range(1, attempts + 1):
            client = self._prepare_client()
            if client.connected:
                return client
            try:
                async with asyncio.timeout(_CONNECT_ATTEMPT_TIMEOUT_SECONDS):
                    await client.connect()
            except Exception as err:
                if isinstance(err, TimeoutError):
                    message = f"timed out after {_CONNECT_ATTEMPT_TIMEOUT_SECONDS:.0f} s"
                else:
                    message = str(err) or type(err).__name__
                if attempt < attempts:
                    _LOGGER.debug(
                        "Connection attempt %s/%s to %s failed, retrying: %s",
                        attempt,
                        attempts,
                        self.address,
                        message,
                    )
                    continue
                self._connect_failed_at = loop.time()
                raise ConnectionFailed(
                    f"Could not connect to Bluetooth device {self.address} "
                    f"after {attempts} attempt(s): {message}"
                ) from err
            self._connect_failed_at = None
            return client
        raise AssertionError("unreachable")

    def _prepare_client(self) -> LightClient:
        """Point the client at Home Assistant's current best route to the device."""
        if current_device := self._current_ble_device():
            self._ble_device = current_device
        if self._ble_device is None:
            raise UpdateFailed(f"Bluetooth device {self.address} is not currently reachable")
        if self.client is None:
            self.client = LightClient(
                self._ble_device,
                self._advertisement,
                client_factory=self._async_client_factory,
            )
        else:
            self.client.set_ble_device(self._ble_device, self._advertisement)
        self.client.curtain_calibration = self.calibration
        return self.client

    def _connect_cooldown_active(self) -> bool:
        if self._connect_failed_at is None or (self.client and self.client.connected):
            return False
        elapsed = asyncio.get_running_loop().time() - self._connect_failed_at
        return elapsed < _CONNECT_COOLDOWN_SECONDS

    def _clear_assumed_rgb(self) -> None:
        self._assumed_rgb = None
        self._assumed_rgb_brightness = None
        self._assumed_rgb_deadline = None

    def _reconcile_assumed_rgb(self, state: LightState) -> LightState:
        if self._assumed_rgb is None or self._assumed_rgb_brightness is None:
            state.assumed = False
            return state
        if rgb_state_matches(state, self._assumed_rgb, self._assumed_rgb_brightness):
            self._clear_assumed_rgb()
            state.assumed = False
            return state
        deadline = self._assumed_rgb_deadline
        if deadline is None or asyncio.get_running_loop().time() >= deadline:
            self._clear_assumed_rgb()
            state.assumed = False
            return state
        return apply_assumed_rgb(
            state,
            self._assumed_rgb,
            self._assumed_rgb_brightness,
        )

    @property
    def is_curtain(self) -> bool:
        """True once the connected device is resolved as an IOTBT curtain (product 0xC2)."""
        return bool(self.client and self.client.is_curtain)

    def _apply_curtain_assumed(self, state: LightState) -> LightState:
        """Add the color and brightness the curtain never reports.

        The last command sent is used: brightness always, and the color only while the
        device reports color mode. Effects and animations carry no color.
        """
        client = self.client
        if client is None or not client.is_curtain:
            return state
        if state.mode == curtain_c2.MODE_DIY and state.speed:
            self._last_animation_speed = state.speed
        percent = client.curtain_percent
        if percent is not None:
            state.brightness = round(percent * 255 / 100)
            state.assumed = True
        if (
            state.mode == curtain_c2.MODE_COLOR
            and client.curtain_rgb is not None
            and percent is not None
        ):
            state.rgb = client.curtain_rgb
            state.white = 0
        return state

    async def _refresh_after_assumed_deadline(self, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        if self._assumed_rgb_deadline == deadline:
            await self.async_request_refresh()

    async def _refresh_after_command(self, generation: int) -> None:
        await asyncio.sleep(_CONFIRMATION_DELAY_SECONDS)
        if self._confirmation_generation == generation:
            await self.async_request_refresh()

    def _request_confirmation(self) -> None:
        self._confirmation_generation += 1
        self.hass.async_create_task(
            self._refresh_after_command(self._confirmation_generation)
        )
        if self._assumed_rgb_deadline is not None:
            self.hass.async_create_task(
                self._refresh_after_assumed_deadline(self._assumed_rgb_deadline)
            )

    async def _async_reset_connection(self, *, clear_cache: bool = False) -> None:
        """Drop the BLE connection so the next operation reconnects from scratch."""
        if self.client is None:
            return
        if clear_cache:
            await self.client.clear_cache()
        with suppress(Exception):
            async with asyncio.timeout(_DISCONNECT_TIMEOUT_SECONDS):
                await self.client.disconnect()

    def _probe_backoff_active(self) -> bool:
        return (
            self._next_probe_at is not None
            and asyncio.get_running_loop().time() < self._next_probe_at
        )

    def _record_probe_failure(self) -> None:
        self._probe_failures += 1
        delay = min(
            _PROBE_BACKOFF_BASE_SECONDS * 2 ** (self._probe_failures - 1),
            _PROBE_BACKOFF_MAX_SECONDS,
        )
        self._next_probe_at = asyncio.get_running_loop().time() + delay

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._probe_failures = 0
        self._next_probe_at = None
        if not self._was_available:
            _LOGGER.info("Surplife BLE device %s is now available", self.address)
            self._was_available = True

    @staticmethod
    def _operation_timeout(client: LightClient) -> float:
        return _OPERATION_TIMEOUT_SECONDS if client.resolved else _PROBE_TIMEOUT_SECONDS

    async def _async_poll(self) -> LightState:
        client = await self._async_get_client()
        was_resolved = client.resolved
        async with asyncio.timeout(self._operation_timeout(client)):
            # Once resolved, the lamp answers on FF02 notifications; polling every
            # readable characteristic after a slow reply only adds GATT traffic.
            state = await client.query_state(allow_reads=not was_resolved)
        if state is None:
            raise UpdateFailed("device did not return a supported state frame")
        if not client.resolution or not client.resolution.repeatable:
            raise UpdateFailed("device state response could not be repeated")
        return state

    async def _async_update_data(self) -> LightState:
        async with self._operation_lock:
            client = self.client
            if client is not None and not client.resolved and self._probe_backoff_active():
                # Probing writes a burst of guessed frames. Repeating it on every
                # poll keeps the lamp busy and can make it disconnect.
                raise UpdateFailed("waiting before identifying the device again")
            try:
                state = await self._async_poll()
            except Exception as err:
                return await self._async_handle_poll_failure(err)
            self._record_success()
            return self._apply_curtain_assumed(self._reconcile_assumed_rgb(state))

    async def _async_handle_poll_failure(self, err: Exception) -> LightState:
        self._consecutive_failures += 1
        if (
            self.client is not None
            and not self.client.resolved
            and not isinstance(err, ConnectionFailed)
        ):
            # Only a probe that ran and failed backs off. A lamp that could not
            # be reached was never sent probe frames, so retry it on the next poll.
            self._record_probe_failure()
        if isinstance(err, BleakError):
            # Stale cached services make every write fail the same way, so drop
            # them along with the link.
            await self._async_reset_connection(clear_cache=True)
        elif self._consecutive_failures >= _RECONNECT_AFTER_FAILURES:
            _LOGGER.debug(
                "Surplife BLE device %s failed %s polls in a row; reconnecting",
                self.address,
                self._consecutive_failures,
            )
            await self._async_reset_connection()

        if self.data is not None and self._consecutive_failures < _TOLERATED_FAILURES:
            _LOGGER.debug(
                "Surplife BLE poll of %s failed (%s/%s), keeping last state: %s",
                self.address,
                self._consecutive_failures,
                _TOLERATED_FAILURES,
                err,
            )
            return replace(self.data, assumed=True)

        if self._was_available:
            _LOGGER.warning("Surplife BLE device %s is unavailable: %s", self.address, err)
            self._was_available = False
        if isinstance(err, UpdateFailed):
            raise err
        raise UpdateFailed(str(err) or type(err).__name__) from err

    async def _async_run_command(self, method: str, *args: Any, **kwargs: Any) -> None:
        client = await self._async_get_client(attempts=_COMMAND_CONNECT_ATTEMPTS)
        timeout = (
            _UPLOAD_TIMEOUT_SECONDS
            if method == "upload_curtain_animation"
            else self._operation_timeout(client)
        )
        async with asyncio.timeout(timeout):
            if not client.resolution or not client.resolution.repeatable:
                await client.query_state()
            await getattr(client, method)(*args, **kwargs)

    async def _async_command(self, method: str, *args: Any, **kwargs: Any) -> LightState:
        async with self._operation_lock:
            if self._connect_cooldown_active():
                # Commands queued behind a failed connection would each make
                # their own attempt. Fail them now; the next poll reconnects.
                raise ConnectionFailed(
                    f"Bluetooth device {self.address} is not reachable right now"
                )
            try:
                await self._async_run_command(method, *args, **kwargs)
            except ConnectionFailed:
                # Connecting already used all of its attempts. Retrying would
                # double how long the command and everything queued behind it wait.
                raise
            except (BleakError, TimeoutError, OSError) as err:
                _LOGGER.debug("Command %s failed, reconnecting and retrying: %s", method, err)
                await self._async_reset_connection(clear_cache=isinstance(err, BleakError))
                await self._async_run_command(method, *args, **kwargs)
            assert self.client is not None and self.client.state is not None
            self._record_success()
            self.async_set_updated_data(self.client.state)
            return self.client.state

    async def async_set_power(self, turn_on: bool) -> None:
        await self._async_command("set_power", turn_on)

    async def async_set_rgb(
        self, rgb: tuple[int, int, int], brightness: int | None
    ) -> None:
        if brightness is None:
            brightness = 255
        state = await self._async_command(
            "set_rgb", *rgb, brightness=max(1, round(brightness / 255 * 100))
        )
        if self.is_curtain:
            self._request_confirmation()
            return
        if state.assumed and state.rgb is not None and state.brightness is not None:
            self._assumed_rgb = state.rgb
            self._assumed_rgb_brightness = state.brightness
            self._assumed_rgb_deadline = (
                asyncio.get_running_loop().time() + _ASSUMED_RGB_SECONDS
            )
            self._request_confirmation()
        else:
            self._clear_assumed_rgb()

    async def async_set_white(self, brightness: int | None) -> None:
        if brightness is None:
            brightness = 255
        await self._async_command("set_white", max(1, round(brightness / 255 * 100)))
        self._clear_assumed_rgb()
        self._request_confirmation()

    async def async_set_brightness(self, brightness: int | None) -> None:
        if brightness is None:
            brightness = 255
        state = await self._async_command(
            "set_brightness", max(1, round(brightness / 255 * 100))
        )
        if self.is_curtain:
            self._request_confirmation()
            return
        if state.assumed:
            if not state.white and state.rgb is not None and state.brightness is not None:
                self._assumed_rgb = state.rgb
                self._assumed_rgb_brightness = state.brightness
                self._assumed_rgb_deadline = (
                    asyncio.get_running_loop().time() + _ASSUMED_RGB_SECONDS
                )
            else:
                self._clear_assumed_rgb()
            self._request_confirmation()
        else:
            self._clear_assumed_rgb()

    def _curtain_speed(self) -> int:
        """Reuse the speed the device reports for an effect or animation, else 50."""
        state = self.data
        if state is not None and state.speed:
            return max(1, min(100, state.speed))
        return 50

    def curtain_percent(self, brightness_percent: int | None = None) -> int:
        """Use ``brightness_percent``, else the last brightness sent, else full."""
        if brightness_percent is not None:
            return max(1, min(100, brightness_percent))
        client = self.client
        return client.curtain_percent if client and client.curtain_percent else 100

    def _curtain_play_mode(self) -> int:
        """Keep the play mode the device reports while an animation plays, else mode 1."""
        state = self.data
        if state is not None and state.mode == curtain_c2.MODE_DIY and state.play_mode is not None:
            return state.play_mode
        return curtain_c2.DEFAULT_PLAY_MODE

    @property
    def animation_speed(self) -> int:
        """Speed of the playing animation, or the last one seen or sent (default 50)."""
        state = self.data
        if state is not None and state.mode == curtain_c2.MODE_DIY and state.speed:
            return state.speed
        return self._last_animation_speed

    async def async_set_effect(
        self, effect_id: int, brightness_percent: int | None = None
    ) -> None:
        """Start a built-in curtain effect."""
        await self._async_command(
            "set_effect",
            effect_id,
            self._curtain_speed(),
            self.curtain_percent(brightness_percent),
        )
        self._request_confirmation()

    async def async_play_animation(
        self,
        mode: int | None = None,
        speed: int | None = None,
        brightness_percent: int | None = None,
    ) -> None:
        """Play the animation stored in the curtain's flash (``E2 09``).

        Mode 0 and 1 loop all frames and mode 2 and up hold frame 1. Missing arguments
        keep the current play mode and speed, and the last brightness.
        """
        speed = speed if speed is not None else self.animation_speed
        self._last_animation_speed = speed
        await self._async_command(
            "play_animation",
            mode if mode is not None else self._curtain_play_mode(),
            speed,
            self.curtain_percent(brightness_percent),
        )
        self._request_confirmation()

    async def async_upload_animation(
        self,
        frames: list[list[tuple[int, int, int]]],
        *,
        mode: int,
        speed: int,
        brightness_percent: int,
        title: str | None = None,
        preview_png: bytes | None = None,
    ) -> None:
        """Write frames to the curtain's flash and play them. This wears the flash.

        ``title`` and ``preview_png`` are kept in memory for the Screen entities.
        """
        await self._async_command(
            "upload_curtain_animation",
            frames,
            mode=mode,
            speed=speed,
            brightness=brightness_percent,
        )
        self._last_animation_speed = speed
        self.animation_title = title
        self.preview_png = preview_png
        self.preview_updated = datetime.now(UTC)
        self._request_confirmation()
        self.async_update_listeners()

    async def async_disconnect(self) -> None:
        await self._async_reset_connection()
