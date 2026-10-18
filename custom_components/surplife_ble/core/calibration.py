"""Color calibration for LED curtains.

The IOTBT curtain stores colors as coarse HSV (hue in 3 degree steps, 4 bits of saturation,
5 bits of value) and its LEDs render colors differently from a screen: green is strong and
partly saturated colors look pale. The calibration is deliberately minimal: a gain per RGB
channel and one gamma value, applied to the RGB color before it is converted to HSV.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

GAIN_MIN = 0.0
GAIN_MAX = 2.0
GAMMA_MIN = 0.2
GAMMA_MAX = 4.0

FIELDS = ("red_gain", "green_gain", "blue_gain", "gamma")


class CalibrationError(ValueError):
    """An invalid calibration value or an unreadable calibration file."""


def _check(name: str, value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CalibrationError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if math.isnan(number) or not low <= number <= high:
        raise CalibrationError(f"{name} must be between {low:g} and {high:g}, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class ColorCalibration:
    """Per-channel gains and a gamma. The default is the identity (no change)."""

    red_gain: float = 1.0
    green_gain: float = 1.0
    blue_gain: float = 1.0
    gamma: float = 1.0

    def __post_init__(self) -> None:
        for name in ("red_gain", "green_gain", "blue_gain"):
            object.__setattr__(self, name, _check(name, getattr(self, name), GAIN_MIN, GAIN_MAX))
        object.__setattr__(self, "gamma", _check("gamma", self.gamma, GAMMA_MIN, GAMMA_MAX))

    @property
    def is_identity(self) -> bool:
        return (self.red_gain, self.green_gain, self.blue_gain, self.gamma) == (1.0, 1.0, 1.0, 1.0)

    def apply(self, red: int, green: int, blue: int) -> tuple[int, int, int]:
        """Return the calibrated color: ``c' = clamp(round(255 * min(1, (c/255)**gamma * gain)))``.

        The identity returns the input unchanged.
        """
        if self.is_identity:
            return red, green, blue
        return (
            self._channel(red, self.red_gain),
            self._channel(green, self.green_gain),
            self._channel(blue, self.blue_gain),
        )

    def _channel(self, value: int, gain: float) -> int:
        scaled = ((max(0, min(255, value)) / 255) ** self.gamma) * gain
        return max(0, min(255, round(255 * min(1.0, scaled))))

    def with_changes(self, **changes: float) -> ColorCalibration:
        """Return a copy with some fields replaced. Raises CalibrationError if invalid."""
        unknown = set(changes) - set(FIELDS)
        if unknown:
            raise CalibrationError(f"unknown calibration field: {', '.join(sorted(unknown))}")
        return replace(self, **changes)

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ColorCalibration:
        """Build a calibration from a mapping. Missing fields keep their defaults."""
        if not isinstance(data, dict):
            raise CalibrationError("a calibration must be a JSON object")
        unknown = set(data) - set(FIELDS)
        if unknown:
            raise CalibrationError(
                f"unknown calibration field: {', '.join(sorted(map(str, unknown)))} "
                f"(expected {', '.join(FIELDS)})"
            )
        return cls(**data)

    @classmethod
    def load(cls, path: str | Path) -> ColorCalibration:
        """Read a calibration from a JSON file."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as err:
            raise CalibrationError(f"cannot read calibration file {path}: {err}") from err
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise CalibrationError(f"{path} is not valid JSON: {err}") from err
        try:
            return cls.from_dict(data)
        except CalibrationError as err:
            raise CalibrationError(f"{path}: {err}") from err

    def save(self, path: str | Path) -> None:
        """Write the calibration to a JSON file."""
        try:
            Path(path).write_text(
                json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
        except OSError as err:
            raise CalibrationError(f"cannot write calibration file {path}: {err}") from err


IDENTITY = ColorCalibration()
