"""Interactive color calibration for the IOTBT curtain (`ble-rgb curtain calibrate`).

The session shows reference swatches on the whole curtain with the color command (3B A1,
cmdId 0x0B). It never writes to flash. After each change it sends the current swatch again
with the calibration applied. The line-based input and the output are injected, so the
session can be scripted in tests.
"""

from __future__ import annotations

import colorsys
from collections.abc import Awaitable, Callable
from pathlib import Path

from custom_components.surplife_ble.core import curtain_c2
from custom_components.surplife_ble.core.calibration import (
    FIELDS,
    CalibrationError,
    ColorCalibration,
)

RGB = tuple[int, int, int]

# Reference colors. Orange, yellow, olive and warm white are what the LEDs render well:
# fully saturated, with one channel at 0 where possible.
SWATCHES: list[tuple[str, RGB]] = [
    ("white", (255, 255, 255)),
    ("red", (255, 0, 0)),
    ("orange", (255, 60, 0)),  # #FF3C00, hue about 14 degrees
    ("yellow", (255, 180, 0)),  # #FFB400
    ("green", (0, 255, 0)),
    ("olive", (90, 120, 0)),  # #5A7800, hue about 75 degrees, darker
    ("cyan", (0, 255, 255)),
    ("blue", (0, 0, 255)),
    ("purple", (128, 0, 255)),
    ("warm white", (255, 180, 110)),  # #FFB46E
]

GAIN_STEP = 0.05
GAMMA_STEP = 0.1

HELP = """\
Commands:
  next, prev          show the next or previous reference swatch
  color R,G,B         show any color (0-255 per channel)
  r+ r- g+ g- b+ b-   change a channel gain by 0.05
  gamma+ gamma-       change the gamma by 0.1
  set NAME VALUE      set red_gain, green_gain, blue_gain or gamma, e.g. set red_gain 0.8
  show                print the calibration and the current swatch
  list                list the reference swatches
  save [FILE]         write the calibration to a JSON file
  help, quit"""

_ADJUSTMENTS: dict[str, tuple[str, float]] = {
    "r+": ("red_gain", GAIN_STEP),
    "r-": ("red_gain", -GAIN_STEP),
    "g+": ("green_gain", GAIN_STEP),
    "g-": ("green_gain", -GAIN_STEP),
    "b+": ("blue_gain", GAIN_STEP),
    "b-": ("blue_gain", -GAIN_STEP),
    "gamma+": ("gamma", GAMMA_STEP),
    "gamma-": ("gamma", -GAMMA_STEP),
}


def describe_swatch(name: str, raw: RGB, calibration: ColorCalibration) -> tuple[str, int]:
    """Return a one-line description and the brightness percent sent for a swatch.

    The brightness follows the value of the calibrated color, so a dark color such as
    olive looks dark, as it would in an upload. The line shows the raw and calibrated RGB,
    the hue and saturation of the color command, its packed 16-bit value, and the 16-bit
    HSV pixel an upload would store.
    """
    calibrated = calibration.apply(*raw)
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in calibrated))
    hue, saturation = round(h * 360) % 360, round(s * 100)
    brightness = max(1, round(v * 100))
    packed = (hue << 7) | saturation
    line = (
        f"{name}: raw {raw} -> calibrated {calibrated}  "
        f"hue {hue}\N{DEGREE SIGN} sat {saturation}% brightness {brightness}%  "
        f"command hs16 0x{packed:04x}, pixel16 0x{curtain_c2.pixel_hsv16(*calibrated):04x}"
    )
    return line, brightness


class CalibrationSession:
    """Drive the swatches and the calibration from line-based commands."""

    def __init__(
        self,
        send: Callable[[curtain_c2.CurtainCommand], Awaitable[None]],
        calibration: ColorCalibration,
        *,
        save_path: str | None = None,
        out: Callable[[str], None] = print,
    ) -> None:
        self._send = send
        self.calibration = calibration
        self._save_path = save_path
        self._out = out
        self._index = 0
        self._name, self._raw = SWATCHES[0]
        self._dirty = False

    async def run(self, ask: Callable[[str], Awaitable[str]]) -> None:
        """Run until `quit` or the end of the input."""
        self._out("Color calibration. Swatches use the color command; nothing is written to flash.")
        self._out("Turn the curtain on first.")
        self._out(HELP)
        await self.show_swatch()
        while True:
            try:
                line = await ask("calibrate> ")
            except EOFError:
                break
            if not await self.handle(line):
                break
        if self._dirty:
            self._out("The calibration was changed but not saved. Use `save FILE` next time.")

    async def show_swatch(self) -> None:
        """Send the current swatch with the calibration applied and describe it."""
        line, brightness = describe_swatch(self._name, self._raw, self.calibration)
        await self._send(curtain_c2.color_rgb(*self._raw, brightness, calibration=self.calibration))
        self._out(line)

    async def handle(self, line: str) -> bool:
        """Run one command. Returns False to end the session."""
        words = line.strip().split()
        if not words:
            return True
        command, args = words[0].lower(), words[1:]
        try:
            if command in ("quit", "q", "exit"):
                return False
            if command in ("help", "?"):
                self._out(HELP)
            elif command in ("next", "n", "prev", "p"):
                step = 1 if command in ("next", "n") else -1
                self._index = (self._index + step) % len(SWATCHES)
                self._name, self._raw = SWATCHES[self._index]
                await self.show_swatch()
            elif command == "color":
                self._custom_color(args)
                await self.show_swatch()
            elif command in _ADJUSTMENTS:
                field, delta = _ADJUSTMENTS[command]
                self._change(field, getattr(self.calibration, field) + delta)
                await self.show_swatch()
            elif command == "set":
                self._set(args)
                await self.show_swatch()
            elif command == "show":
                self._out(self._calibration_text())
                self._out(describe_swatch(self._name, self._raw, self.calibration)[0])
            elif command == "list":
                for index, (name, raw) in enumerate(SWATCHES, 1):
                    self._out(f"  {index:2d}. {name} {raw}")
            elif command == "save":
                self._save(args)
            else:
                self._out(f"unknown command: {command}. Type `help` for the list.")
        except CalibrationError as err:
            self._out(f"error: {err}")
        return True

    def _calibration_text(self) -> str:
        values = ", ".join(f"{k}={v:g}" for k, v in self.calibration.to_dict().items())
        return f"calibration: {values}"

    def _change(self, field: str, value: float) -> None:
        self.calibration = self.calibration.with_changes(**{field: round(value, 4)})
        self._dirty = True

    def _custom_color(self, args: list[str]) -> None:
        if len(args) != 1:
            raise CalibrationError("usage: color R,G,B")
        try:
            channels = tuple(int(part) for part in args[0].split(","))
        except ValueError as err:
            raise CalibrationError("usage: color R,G,B (whole numbers 0-255)") from err
        if len(channels) != 3 or any(not 0 <= c <= 255 for c in channels):
            raise CalibrationError("usage: color R,G,B (three numbers from 0 to 255)")
        self._name, self._raw = "custom", (channels[0], channels[1], channels[2])

    def _set(self, args: list[str]) -> None:
        if len(args) != 2 or args[0] not in FIELDS:
            raise CalibrationError(f"usage: set NAME VALUE, with NAME one of {', '.join(FIELDS)}")
        try:
            value = float(args[1])
        except ValueError as err:
            raise CalibrationError(f"{args[1]!r} is not a number") from err
        self._change(args[0], value)

    def _save(self, args: list[str]) -> None:
        path = args[0] if args else self._save_path
        if not path:
            raise CalibrationError("usage: save FILE (or start with --save FILE)")
        self.calibration.save(Path(path))
        self._save_path = path
        self._dirty = False
        self._out(f"saved {path}: {self._calibration_text()}")
