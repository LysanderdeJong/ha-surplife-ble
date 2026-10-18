"""Color calibration: per-channel gains and a gamma (math, validation and JSON files)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from custom_components.surplife_ble.core import curtain_c2
from custom_components.surplife_ble.core.calibration import (
    IDENTITY,
    CalibrationError,
    ColorCalibration,
)


def test_identity_is_byte_exact_for_every_channel_value() -> None:
    assert IDENTITY == ColorCalibration()
    assert IDENTITY.is_identity
    for value in range(256):
        assert IDENTITY.apply(value, 255 - value, value) == (value, 255 - value, value)


def test_gains_scale_each_channel_and_clamp_at_255() -> None:
    assert ColorCalibration(red_gain=0.8).apply(200, 100, 50) == (160, 100, 50)
    assert ColorCalibration(green_gain=0.5).apply(0, 255, 255) == (0, 128, 255)
    assert ColorCalibration(blue_gain=2.0).apply(10, 10, 200) == (10, 10, 255)  # clamped
    assert ColorCalibration(red_gain=0.0).apply(255, 255, 255) == (0, 255, 255)
    assert not ColorCalibration(red_gain=0.8).is_identity


def test_gamma_is_applied_before_the_gain() -> None:
    assert ColorCalibration(gamma=2.0).apply(255, 120, 0) == (255, 56, 0)
    assert ColorCalibration(gamma=2.0).apply(128, 128, 128) == (64, 64, 64)
    assert ColorCalibration(gamma=0.5).apply(64, 64, 64) == (128, 128, 128)
    # 255 and 0 are fixed points of the gamma curve.
    assert ColorCalibration(gamma=3.0).apply(255, 0, 255) == (255, 0, 255)
    both = ColorCalibration(green_gain=0.9, gamma=2.0)
    assert both.apply(0, 200, 0) == (0, round(255 * (200 / 255) ** 2 * 0.9), 0)


def test_apply_matches_the_formula_for_all_values() -> None:
    calibration = ColorCalibration(1.2, 0.7, 0.9, 1.6)
    for value in range(256):
        red, green, blue = calibration.apply(value, value, value)
        for gain, got in ((1.2, red), (0.7, green), (0.9, blue)):
            expected = max(0, min(255, round(255 * min(1.0, (value / 255) ** 1.6 * gain))))
            assert got == expected


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"red_gain": -0.01}, "red_gain must be between 0 and 2"),
        ({"green_gain": 2.01}, "green_gain must be between 0 and 2"),
        ({"blue_gain": 5}, "blue_gain"),
        ({"gamma": 0.19}, "gamma must be between 0.2 and 4"),
        ({"gamma": 4.01}, "gamma must be between 0.2 and 4"),
        ({"gamma": math.nan}, "gamma"),
        ({"red_gain": "1.0"}, "must be a number"),
        ({"red_gain": True}, "must be a number"),
        ({"gamma": None}, "must be a number"),
    ],
)
def test_invalid_values_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(CalibrationError, match=message):
        ColorCalibration(**kwargs)  # type: ignore[arg-type]


def test_limits_are_accepted_and_integers_become_floats() -> None:
    calibration = ColorCalibration(0, 2, 1, 0.2)
    assert calibration.to_dict() == {
        "red_gain": 0.0,
        "green_gain": 2.0,
        "blue_gain": 1.0,
        "gamma": 0.2,
    }
    assert isinstance(calibration.red_gain, float)
    assert ColorCalibration(gamma=4).gamma == 4.0
    assert issubclass(CalibrationError, ValueError)


def test_with_changes_validates_and_rejects_unknown_fields() -> None:
    changed = IDENTITY.with_changes(red_gain=0.5, gamma=1.5)
    assert (changed.red_gain, changed.gamma) == (0.5, 1.5)
    assert IDENTITY.red_gain == 1.0  # frozen
    with pytest.raises(CalibrationError, match="red_gain"):
        IDENTITY.with_changes(red_gain=2.5)
    with pytest.raises(CalibrationError, match="unknown calibration field: hue"):
        IDENTITY.with_changes(hue=1.0)


def test_dict_round_trip_and_defaults() -> None:
    calibration = ColorCalibration(0.8, 0.6, 1.1, 1.3)
    assert ColorCalibration.from_dict(calibration.to_dict()) == calibration
    assert ColorCalibration.from_dict({}) == IDENTITY
    assert ColorCalibration.from_dict({"gamma": 2}) == ColorCalibration(gamma=2.0)
    with pytest.raises(CalibrationError, match="unknown calibration field: brightness"):
        ColorCalibration.from_dict({"brightness": 1.0})
    with pytest.raises(CalibrationError, match="JSON object"):
        ColorCalibration.from_dict([1, 2])  # type: ignore[arg-type]


def test_json_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    calibration = ColorCalibration(0.8, 0.55, 1.0, 1.4)
    calibration.save(path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "red_gain": 0.8,
        "green_gain": 0.55,
        "blue_gain": 1.0,
        "gamma": 1.4,
    }
    assert ColorCalibration.load(path) == calibration


def test_load_errors_name_the_file(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError, match="cannot read calibration file"):
        ColorCalibration.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(CalibrationError, match=r"bad\.json is not valid JSON"):
        ColorCalibration.load(bad)
    out_of_range = tmp_path / "range.json"
    out_of_range.write_text('{"gamma": 9}', encoding="utf-8")
    with pytest.raises(CalibrationError, match=r"range\.json: gamma must be between"):
        ColorCalibration.load(out_of_range)
    unknown = tmp_path / "unknown.json"
    unknown.write_text('{"hue_shift": 1}', encoding="utf-8")
    with pytest.raises(CalibrationError, match="unknown calibration field: hue_shift"):
        ColorCalibration.load(unknown)
    with pytest.raises(CalibrationError, match="cannot write calibration file"):
        IDENTITY.save(tmp_path / "no" / "such" / "dir.json")


def test_color_command_uses_the_calibrated_color() -> None:
    green_half = ColorCalibration(green_gain=0.5)
    plain = curtain_c2.color_rgb(0, 255, 255, 100)
    calibrated = curtain_c2.color_rgb(0, 255, 255, 100, calibration=green_half)
    assert plain.payload.hex() == "3ba15a646400000000000000fe"  # hue 180
    assert calibrated.payload.hex() == "3ba1696464000000000000000d"  # (0, 128, 255): hue 210
    assert curtain_c2.color_rgb(0, 255, 255, 100, calibration=IDENTITY) == plain
    assert curtain_c2.color_rgb(0, 255, 255, 100, calibration=None) == plain
    assert calibrated.cmd_id == 0x0B
    gamma = ColorCalibration(gamma=2.0)
    assert curtain_c2.color_rgb(255, 120, 0, 100, calibration=gamma).payload.hex() == (
        "3ba106e464000000000000002a"
    )


def test_calibration_changes_the_uploaded_pixels_only_when_set() -> None:
    frame = [(255, 120, 0)] * 400
    plain = curtain_c2.frame_blob(frame)
    assert plain == bytes.fromhex("13ff") * 400
    assert curtain_c2.frame_blob(frame, IDENTITY) == plain
    assert curtain_c2.frame_blob(frame, None) == plain
    assert curtain_c2.frame_blob(frame, ColorCalibration(gamma=2.0)) == bytes.fromhex("09ff") * 400


def test_frame_blob_calibrates_each_distinct_pixel() -> None:
    calibration = ColorCalibration(green_gain=0.5)
    frame = [(0, 255, 255)] * 200 + [(255, 0, 0)] * 200
    expected = b"".join(
        curtain_c2.pixel_hsv16(*calibration.apply(*pixel)).to_bytes(2, "big") for pixel in frame
    )
    assert curtain_c2.frame_blob(frame, calibration) == expected


def test_build_upload_applies_the_calibration() -> None:
    frames = [[(255, 120, 0)] * 400]
    plain = curtain_c2.build_upload(frames)
    same = curtain_c2.build_upload(frames, IDENTITY)
    assert [step.payload for step in same] == [step.payload for step in plain]
    calibrated = curtain_c2.build_upload(frames, ColorCalibration(gamma=2.0))
    assert calibrated[0].payload == plain[0].payload  # header: same frame count
    assert calibrated[2].payload != plain[2].payload
    assert calibrated[2].payload[4:] == curtain_c2.lz4_block(bytes.fromhex("09ff") * 400)
