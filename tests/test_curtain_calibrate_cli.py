"""`ble-rgb curtain calibrate` and `--calibration` on the curtain color and upload commands."""

from __future__ import annotations

import argparse
import builtins
import colorsys
import json
from pathlib import Path

import pytest
from curtain_fakes import FF01, FakeCurtain, _Device

import surplife_ble.cli as cli
from custom_components.surplife_ble.core import curtain_c2
from custom_components.surplife_ble.core.calibration import IDENTITY, ColorCalibration
from custom_components.surplife_ble.core.client import LightClient
from custom_components.surplife_ble.core.framing import with_checksum, wrap_magichome
from surplife_ble.cli import build_parser


def patch_device(monkeypatch: pytest.MonkeyPatch, fake: FakeCurtain) -> None:
    async def fake_device_client(args: argparse.Namespace) -> LightClient:
        return LightClient(
            _Device(),  # type: ignore[arg-type]
            capture_path=getattr(args, "jsonl", None),
            client_factory=lambda _device: fake,
        )

    monkeypatch.setattr(cli, "_device_client", fake_device_client)


def calibrate_args(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        ["curtain", "calibrate", "--device", "CURTAIN", "--allow-curtain-c2", *extra]
    )


def expected_frame(
    raw: tuple[int, int, int], calibration: ColorCalibration, sequence: int
) -> bytes:
    """The FF01 frame for a swatch: 3B A1 hs16 brightness (the value of the calibrated color)."""
    red, green, blue = calibration.apply(*raw)
    h, s, v = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    packed = (round(h * 360) % 360) << 7 | round(s * 100)
    inner = with_checksum(
        bytes((0x3B, 0xA1, packed >> 8, packed & 0xFF, max(1, round(v * 100)), 0, 0, 0, 0, 0, 0, 0))
    )
    return wrap_magichome(inner, sequence, command_family=0x0B)


class Script:
    """Scripted input for the interactive session. It ends with EOF."""

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


@pytest.mark.asyncio
async def test_calibrate_session_sends_exact_color_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    saved = tmp_path / "cal.json"
    script = Script(
        "next",  # red
        "next",  # orange
        "g-",
        "gamma+",
        "color 255,120,0",
        "set red_gain 3",  # out of range: error, nothing is sent
        "r+",
        "set gamma 1.5",
        "prev",  # red
        "show",  # nothing is sent
        "bogus",  # nothing is sent
        "save",  # no file name: error
        f"save {saved}",
        "quit",
    )
    lines: list[str] = []
    assert await cli._curtain_calibrate(calibrate_args(), ask=script, out=lines.append) == 0

    orange = (255, 60, 0)
    custom = (255, 120, 0)
    steps: list[tuple[tuple[int, int, int], ColorCalibration]] = []
    calibration = IDENTITY
    steps.append(((255, 255, 255), calibration))  # initial swatch: white
    steps.append(((255, 0, 0), calibration))  # next
    steps.append((orange, calibration))  # next
    calibration = calibration.with_changes(green_gain=0.95)
    steps.append((orange, calibration))  # g-
    calibration = calibration.with_changes(gamma=1.1)
    steps.append((orange, calibration))  # gamma+
    steps.append((custom, calibration))  # color 255,120,0
    calibration = calibration.with_changes(red_gain=1.05)
    steps.append((custom, calibration))  # r+
    calibration = calibration.with_changes(gamma=1.5)
    steps.append((custom, calibration))  # set gamma 1.5
    steps.append(((255, 0, 0), calibration))  # prev
    assert calibration == ColorCalibration(1.05, 0.95, 1.0, 1.5)

    assert [data for _uuid, data in fake.writes] == [
        expected_frame(raw, cal, sequence) for sequence, (raw, cal) in enumerate(steps, 1)
    ]
    assert {uuid for uuid, _ in fake.writes} == {FF01}
    # Only color commands: no uploads (EA xx), no playback (E2 09) and no power commands.
    assert {payload[:2] for payload in fake.payloads} == {b"\x3b\xa1"}
    assert json.loads(saved.read_text(encoding="utf-8")) == calibration.to_dict()

    text = "\n".join(lines)
    assert "nothing is written to flash" in text
    assert "orange: raw (255, 60, 0) -> calibrated (255, 60, 0)" in text
    assert "hue 14\N{DEGREE SIGN} sat 100% brightness 100%" in text
    assert "hs16 0x" in text and "pixel16 0x" in text
    assert "error: red_gain must be between 0 and 2" in text
    assert "unknown command: bogus" in text
    assert "error: usage: save FILE" in text
    assert f"saved {saved}: calibration: red_gain=1.05, green_gain=0.95, blue_gain=1" in text
    assert "not saved" not in text  # the changes were saved
    assert "calibration: red_gain=1.05" in text  # `show`
    assert script.prompts[0] == "calibrate> "


@pytest.mark.asyncio
async def test_calibrate_prints_the_hs16_and_pixel16_the_curtain_receives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    lines: list[str] = []
    script = Script("next", "next", "set gamma 2", "quit")
    await cli._curtain_calibrate(calibrate_args(), ask=script, out=lines.append)
    # Orange #FF3C00: hue 14, saturation 100 -> hs16 (14 << 7) | 100 = 0x0764.
    assert any(
        line.startswith("orange: raw (255, 60, 0) -> calibrated (255, 60, 0)")
        and "hs16 0x0764" in line
        and f"pixel16 0x{curtain_c2.pixel_hsv16(255, 60, 0):04x}" in line
        for line in lines
    )
    # Gamma 2 darkens the green channel: (255, 60, 0) -> (255, 14, 0).
    assert any("calibrated (255, 14, 0)" in line for line in lines)


@pytest.mark.asyncio
async def test_calibrate_swatch_brightness_follows_the_calibrated_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    lines: list[str] = []
    # Olive #5A7800 is dark: value 120/255 = 47 %.
    script = Script("color 90,120,0", "quit")
    await cli._curtain_calibrate(calibrate_args(), ask=script, out=lines.append)
    assert fake.payloads[-1][4] == 47
    assert any("brightness 47%" in line for line in lines)


@pytest.mark.asyncio
async def test_calibrate_starts_from_a_file_and_saves_to_the_default_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    start = tmp_path / "start.json"
    ColorCalibration(green_gain=0.5, gamma=1.2).save(start)
    target = tmp_path / "out.json"
    args = calibrate_args("--calibration", str(start), "--save", str(target))
    assert args.calibration == ColorCalibration(green_gain=0.5, gamma=1.2)
    lines: list[str] = []
    await cli._curtain_calibrate(args, ask=Script("b+", "save", "quit"), out=lines.append)
    # The first swatch already uses the starting calibration.
    assert fake.writes[0][1] == expected_frame((255, 255, 255), args.calibration, 1)
    assert ColorCalibration.load(target) == ColorCalibration(
        green_gain=0.5, blue_gain=1.05, gamma=1.2
    )


@pytest.mark.asyncio
async def test_calibrate_limits_and_unsaved_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    lines: list[str] = []
    script = Script("set red_gain 2.0", "r+", "set gamma 0.2", "gamma-", "set gamma x")
    await cli._curtain_calibrate(calibrate_args(), ask=script, out=lines.append)  # ends at EOF
    text = "\n".join(lines)
    assert "error: red_gain must be between 0 and 2, got 2.05" in text
    assert "error: gamma must be between 0.2 and 4, got 0.1" in text
    assert "error: 'x' is not a number" in text
    assert "usage: set NAME VALUE" not in text
    assert "The calibration was changed but not saved" in text
    # Two successful changes (red_gain 2.0, gamma 0.2) plus the first swatch.
    assert len(fake.writes) == 3


@pytest.mark.asyncio
async def test_calibrate_command_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    lines: list[str] = []
    script = Script(
        "color", "color 1,2", "color 1,2,300", "color a,b,c", "set bogus 1", "set gamma"
    )
    await cli._curtain_calibrate(calibrate_args(), ask=script, out=lines.append)
    text = "\n".join(lines)
    assert text.count("error: usage: color R,G,B") == 4
    assert text.count("error: usage: set NAME VALUE") == 2
    assert len(fake.writes) == 1  # only the first swatch


@pytest.mark.asyncio
async def test_calibrate_reads_from_stdin_and_writes_only_to_ff01(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    answers = iter(["next", "r-", "quit"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))
    code = await cli.async_main(
        ["curtain", "calibrate", "--device", "CURTAIN", "--allow-curtain-c2"]
    )
    assert code == 0
    assert "Color calibration." in capsys.readouterr().out
    assert len(fake.writes) == 3
    assert {uuid for uuid, _ in fake.writes} == {FF01}
    assert {payload[:2] for payload in fake.payloads} == {b"\x3b\xa1"}


def test_calibrate_requires_the_curtain_flag_and_a_valid_calibration_file(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["curtain", "calibrate", "--device", "D"])
    bad = tmp_path / "bad.json"
    bad.write_text('{"gamma": 99}', encoding="utf-8")
    for command in (
        ["curtain", "calibrate"],
        ["curtain", "color", "--rgb", "1,2,3"],
        ["curtain", "upload", "--allow-flash-write", "--solid", "1,2,3"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(
                [*command, "--device", "D", "--allow-curtain-c2", "--calibration", str(bad)]
            )
        with pytest.raises(SystemExit):
            parser.parse_args(
                [*command, "--device", "D", "--allow-curtain-c2",
                 "--calibration", str(tmp_path / "missing.json")]
            )


def test_color_command_uses_the_calibration_file(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    ColorCalibration(green_gain=0.5).save(path)
    parser = build_parser()
    base = ["curtain", "color", "--device", "D", "--allow-curtain-c2"]
    plain = cli._curtain_command(parser.parse_args([*base, "--rgb", "0,255,255"]))
    calibrated = cli._curtain_command(
        parser.parse_args([*base, "--rgb", "0,255,255", "--calibration", str(path)])
    )
    assert plain == curtain_c2.color_rgb(0, 255, 255, 100)
    assert calibrated == curtain_c2.color_rgb(
        0, 255, 255, 100, calibration=ColorCalibration(green_gain=0.5)
    )
    assert plain.payload != calibrated.payload  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="only be used with --rgb"):
        cli._curtain_command(
            parser.parse_args([*base, "--hsv", "120,100", "--calibration", str(path)])
        )


@pytest.mark.asyncio
async def test_color_command_sends_the_calibrated_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    path = tmp_path / "cal.json"
    ColorCalibration(green_gain=0.5).save(path)
    code = await cli.async_main(
        ["curtain", "color", "--device", "CURTAIN", "--allow-curtain-c2",
         "--response-timeout", "0.1", "--rgb", "0,255,255", "--calibration", str(path)]
    )
    assert code == 0
    assert fake.writes[0][1][2:] == bytes.fromhex("8000000d0e0b3ba1696464000000000000000d")
    capsys.readouterr()


@pytest.mark.asyncio
async def test_upload_applies_the_calibration_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    patch_device(monkeypatch, fake)
    path = tmp_path / "cal.json"
    ColorCalibration(gamma=2.0).save(path)
    common = [
        "curtain", "upload", "--device", "CURTAIN", "--allow-curtain-c2",
        "--allow-flash-write", "--response-timeout", "0.1", "--solid", "255,120,0", "--no-play",
    ]
    assert await cli.async_main(common) == 0
    plain = [p for p in fake.payloads if p[:2] == b"\xea\x03"]
    fake.payloads.clear()
    assert await cli.async_main([*common, "--calibration", str(path)]) == 0
    calibrated = [p for p in fake.payloads if p[:2] == b"\xea\x03"]
    assert plain == [curtain_c2.build_upload([[(255, 120, 0)] * 400])[2].payload]
    assert calibrated == [
        curtain_c2.build_upload([[(255, 120, 0)] * 400], ColorCalibration(gamma=2.0))[2].payload
    ]
    assert calibrated != plain
    capsys.readouterr()
