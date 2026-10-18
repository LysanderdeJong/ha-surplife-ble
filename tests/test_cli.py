import argparse
import json
import random

import pytest

import surplife_ble.cli as cli
from custom_components.surplife_ble.core import curtain_c2
from custom_components.surplife_ble.core.client import LightClient
from custom_components.surplife_ble.core.framing import wrap_magichome
from surplife_ble.cli import _curtain_command, _hex_bytes, _replay, _rgb, build_parser


def test_cli_surface() -> None:
    parser = build_parser()
    args = parser.parse_args(["color", "--device", "ABC", "--rgb", "1,2,3"])
    assert args.rgb == (1, 2, 3)
    assert args.brightness == 100


def test_raw_write_flag_is_required() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["raw-write", "--device", "ABC", "--characteristic", "ff01", "--hex", "01"]
        )


def test_cli_value_validation() -> None:
    assert _hex_bytes("01:02") == b"\x01\x02"
    with pytest.raises(argparse.ArgumentTypeError):
        _rgb("256,0,0")


def test_hci_convert_subcommand_parses() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "hci-convert", "--snoop", "foo.log", "--device", "DEV",
        "--gatt", "gatt.json", "--jsonl", "out.jsonl",
    ])
    assert args.snoop == "foo.log"
    assert args.device == "DEV"
    assert args.gatt == "gatt.json"
    assert args.jsonl == "out.jsonl"
    assert args.list_connections is False


def test_replay_dry_run_and_limit_parse() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "replay", "--device", "DEV", "--capture", "c.jsonl",
        "--allow-raw-write", "--dry-run", "--limit", "3",
    ])
    assert args.dry_run is True
    assert args.limit == 3


def test_diff_subcommand_parses_hex() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "diff", "--a", "ea810100aa", "--b", "ea810100ab",
        "--label-a", "on", "--label-b", "off",
    ])
    assert args.hex_a == "ea810100aa"
    assert args.hex_b == "ea810100ab"
    assert args.label_a == "on"
    assert args.label_b == "off"


def test_diff_subcommand_parses_captures() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "diff", "--a-capture", "a.jsonl", "--b-capture", "b.jsonl",
    ])
    assert args.capture_a == "a.jsonl"
    assert args.capture_b == "b.jsonl"
    assert args.hex_a is None


@pytest.mark.asyncio
async def test_replay_dry_run_does_not_connect(tmp_path) -> None:
    capture_path = tmp_path / "traffic.jsonl"
    capture_path.write_text(
        json.dumps({
            "device_id": "DEV",
            "direction": "tx",
            "payload_hex": "71230f",
            "characteristic": "0000ff01-0000-1000-8000-00805f9b34fb",
            "note": "power on",
        }) + "\n",
        encoding="utf-8",
    )
    parser = build_parser()
    args = parser.parse_args([
        "replay", "--device", "DEV", "--capture", str(capture_path),
        "--allow-raw-write", "--dry-run",
    ])
    result = await _replay(args)
    assert result == 0


# ---------------------------------------------------------------------------
# `ble-rgb curtain` (IOTBT curtain, product 0xC2)
# ---------------------------------------------------------------------------

from curtain_fakes import (  # noqa: E402
    FF01,
    FF02,
    FF22,
    FakeCurtain,
    _Device,
)


def _patch_device(monkeypatch: pytest.MonkeyPatch, fake: FakeCurtain) -> None:
    async def fake_device_client(args: argparse.Namespace) -> LightClient:
        return LightClient(
            _Device(),  # type: ignore[arg-type]
            capture_path=getattr(args, "jsonl", None),
            client_factory=lambda _device: fake,
        )

    monkeypatch.setattr(cli, "_device_client", fake_device_client)


def _curtain_args(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        ["curtain", *extra[:1], "--device", "CURTAIN", "--allow-curtain-c2",
         "--response-timeout", "0.3", *extra[1:]]
    )


def test_curtain_requires_explicit_safety_flag() -> None:
    parser = build_parser()
    for sub in ("state", "on", "off"):
        with pytest.raises(SystemExit):
            parser.parse_args(["curtain", sub, "--device", "CURTAIN"])
    with pytest.raises(SystemExit):
        parser.parse_args(["curtain", "effect", "--device", "CURTAIN", "--id", "1"])


def test_curtain_parser_options() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "curtain", "color", "--device", "D", "--allow-curtain-c2", "--rgb", "0,0,255",
        "--brightness", "50", "--jsonl", "t.jsonl",
    ])
    assert args.rgb == (0, 0, 255)
    assert args.hsv is None
    assert args.brightness == 50
    assert args.jsonl == "t.jsonl"
    assert args.response_timeout == 1.5
    args = parser.parse_args([
        "curtain", "color", "--device", "D", "--allow-curtain-c2", "--hsv", "240,100",
    ])
    assert args.hsv == (240, 100)
    assert args.brightness == 100
    args = parser.parse_args([
        "curtain", "effect", "--device", "D", "--allow-curtain-c2", "--id", "10",
    ])
    assert (args.effect_id, args.speed, args.brightness) == (10, 50, 100)
    with pytest.raises(SystemExit):  # --rgb and --hsv are exclusive, one is required
        parser.parse_args([
            "curtain", "color", "--device", "D", "--allow-curtain-c2",
            "--rgb", "1,2,3", "--hsv", "1,2",
        ])
    with pytest.raises(SystemExit):
        parser.parse_args(["curtain", "color", "--device", "D", "--allow-curtain-c2"])
    with pytest.raises(SystemExit):
        parser.parse_args([
            "curtain", "color", "--device", "D", "--allow-curtain-c2", "--hsv", "360,0",
        ])


def test_curtain_command_mapping() -> None:
    assert _curtain_command(_curtain_args("state")) is None
    assert _curtain_command(_curtain_args("on")) == curtain_c2.power(True)
    assert _curtain_command(_curtain_args("off")) == curtain_c2.power(False)
    assert _curtain_command(_curtain_args("color", "--hsv", "240,100", "--brightness", "50")) == (
        curtain_c2.color_hsv(240, 100, 50)
    )
    assert _curtain_command(_curtain_args("color", "--rgb", "255,0,0")) == (
        curtain_c2.color_rgb(255, 0, 0, 100)
    )
    assert _curtain_command(
        _curtain_args("effect", "--id", "10", "--speed", "80", "--brightness", "90")
    ) == curtain_c2.effect(10, 80, 90)


@pytest.mark.asyncio
async def test_curtain_state_queries_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        ["curtain", "state", "--device", "CURTAIN", "--allow-curtain-c2",
         "--response-timeout", "0.3"]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["power"] is True
    assert out["product_id"] == "0x00c2"
    assert out["mode_name"] == "initial"
    assert fake.mtu_acquired
    assert fake.writes == [(FF01, wrap_magichome(bytes.fromhex("ea818a8b"), 1))]


@pytest.mark.asyncio
async def test_curtain_off_uses_verified_frame_and_prints_pushed_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        ["curtain", "off", "--device", "CURTAIN", "--allow-curtain-c2",
         "--response-timeout", "0.3"]
    ) == 0
    assert fake.writes == [(FF01, bytes.fromhex("00018000000d0e0b3b240000000000000032000091"))]
    assert json.loads(capsys.readouterr().out)["power"] is False


@pytest.mark.asyncio
async def test_curtain_color_queries_state_when_no_notification_arrives(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        ["curtain", "color", "--device", "CURTAIN", "--allow-curtain-c2",
         "--response-timeout", "0.1", "--hsv", "240,100", "--brightness", "50"]
    ) == 0
    assert fake.writes[0] == (FF01, bytes.fromhex("00018000000d0e0b3ba178643200000000000000ea"))
    assert fake.writes[1] == (FF01, wrap_magichome(bytes.fromhex("ea818a8b"), 2))
    out = json.loads(capsys.readouterr().out)
    assert out["mode_name"] == "color"
    assert out["effect"] is None


@pytest.mark.asyncio
async def test_curtain_effect_prints_scene_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        ["curtain", "effect", "--device", "CURTAIN", "--allow-curtain-c2",
         "--response-timeout", "0.3", "--id", "10", "--speed", "80"]
    ) == 0
    assert fake.writes == [(FF01, bytes.fromhex("000180000006070ae002000a5064"))]
    out = json.loads(capsys.readouterr().out)
    assert (out["mode_name"], out["effect"], out["speed"]) == ("scene", 10, 80)


@pytest.mark.asyncio
async def test_curtain_never_touches_ota_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    for sub in (["on"], ["off"], ["state"], ["effect", "--id", "1"]):
        await cli.async_main(
            ["curtain", sub[0], "--device", "CURTAIN", "--allow-curtain-c2",
             "--response-timeout", "0.1", *sub[1:]]
        )
    capsys.readouterr()
    assert fake.writes
    assert {uuid for uuid, _data in fake.writes} == {FF01}
    assert FF22 not in fake.notify_order
    assert fake.notify_order[0] == FF02


@pytest.mark.asyncio
async def test_curtain_no_state_returns_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()

    async def silent(uuid: str, data: bytes, response: bool | None = None) -> None:
        fake.writes.append((uuid, bytes(data)))

    fake.write_gatt_char = silent  # type: ignore[method-assign]
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        ["curtain", "state", "--device", "CURTAIN", "--allow-curtain-c2",
         "--response-timeout", "0.1"]
    ) == 2
    assert "no EA 81 state" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_unresolved_ea81_codec_stays_locked_in_client() -> None:
    """Only the curtain codec is unlocked: other unresolved EA 81 layouts stay locked."""
    from custom_components.surplife_ble.core.models import ProtocolResolution

    client = LightClient(_Device(), client_factory=lambda _device: FakeCurtain())  # type: ignore[arg-type]
    client.resolution = ProtocolResolution(
        frame_family="extended-ea81",
        codec="unresolved-ea81",
        confidence=0.5,
        repeatable=True,
    )
    client._probe_frame = object()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="locked"):
        client._require_control_resolution()


def _run_args(*extra: str) -> list[str]:
    return ["curtain", extra[0], "--device", "CURTAIN", "--allow-curtain-c2",
            "--response-timeout", "0.3", *extra[1:]]


def test_curtain_upload_requires_both_safety_flags() -> None:
    parser = build_parser()
    base = ["curtain", "upload", "--device", "D", "--solid", "255,0,0"]
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--allow-curtain-c2"])
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--allow-flash-write"])
    with pytest.raises(SystemExit):  # one source is required
        parser.parse_args([
            "curtain", "upload", "--device", "D", "--allow-curtain-c2", "--allow-flash-write",
        ])
    with pytest.raises(SystemExit):  # sources are exclusive
        parser.parse_args([
            *base, "--image", "a.gif", "--allow-curtain-c2", "--allow-flash-write",
        ])


def test_curtain_upload_and_play_options_parse() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "curtain", "upload", "--device", "D", "--allow-curtain-c2", "--allow-flash-write",
        "--solid", "255,0,0;0,0,255",
    ])
    assert args.solid == [(255, 0, 0), (0, 0, 255)]
    assert (args.mode, args.speed, args.brightness) == (1, 50, 100)
    assert args.max_frames == 128
    assert args.resample == "box"
    assert args.controller_side == "left"
    assert not (args.mirror or args.flip or args.no_play)
    args = parser.parse_args([
        "curtain", "upload", "--device", "D", "--allow-curtain-c2", "--allow-flash-write",
        "--image", "a.gif", "--resample", "lanczos", "--max-frames", "5", "--mirror", "--flip",
        "--no-play", "--speed", "20", "--brightness", "10", "--mode", "1",
    ])
    assert args.image == "a.gif"
    assert (args.resample, args.max_frames, args.mirror, args.flip) == ("lanczos", 5, True, True)
    assert (args.no_play, args.speed, args.brightness, args.mode) == (True, 20, 10, 1)
    args = parser.parse_args([
        "curtain", "play", "--device", "D", "--allow-curtain-c2", "--speed", "70",
    ])
    assert _curtain_command(args) == curtain_c2.play(1, 70, 100)
    with pytest.raises(SystemExit):
        parser.parse_args([
            "curtain", "upload", "--device", "D", "--allow-curtain-c2", "--allow-flash-write",
            "--max-frames", "0", "--solid", "1,2,3",
        ])


@pytest.mark.asyncio
async def test_curtain_play_sends_e2_09_and_prints_diy_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(_run_args("play", "--speed", "50")) == 0
    assert fake.writes == [(FF01, wrap_magichome(bytes.fromhex("e20901326400"), 1))]
    out = json.loads(capsys.readouterr().out)
    assert (out["mode_name"], out["play_mode"], out["speed"]) == ("diy", 1, 50)


@pytest.mark.asyncio
async def test_curtain_upload_solid_two_frames_flow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        _run_args("upload", "--allow-flash-write", "--solid", "255,0,0;0,0,255", "--speed", "50")
    ) == 0
    red = "2f01ff0200ffffff0950ff01ff01ff"
    blue = "2fa1ff0200ffffff0950ffa1ffa1ff"
    assert [payload.hex() for payload in fake.payloads[1:]] == [
        "ea0200000000000f0000000f000f",
        "ea030001" + red,
        "ea030002" + blue,
        "ea0400",
        "e20901326400",
    ]
    assert fake.payloads[0][:7].hex() == "ea010100000640"
    assert len(fake.payloads[0]) == 40
    assert {uuid for uuid, _data in fake.writes} == {FF01}
    assert FF22 not in fake.notify_order
    captured = capsys.readouterr()
    assert "header:" in captured.out
    assert "reply: EA 01 status 0x00" in captured.out
    assert "reply: EA 03 status 0x00" in captured.out
    assert "reply: EA 04 status 0x00" in captured.out
    assert "play: mode 1 speed 50 brightness 100" in captured.out
    assert json.loads(captured.out[captured.out.index("{"):])["mode_name"] == "diy"


@pytest.mark.asyncio
async def test_curtain_upload_no_play_only_queries_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        _run_args("upload", "--allow-flash-write", "--solid", "0,255,0", "--no-play")
    ) == 0
    assert not any(payload[:2] == b"\xe2\x09" for payload in fake.payloads)
    assert fake.payloads[-1] == bytes.fromhex("ea818a8b")
    capsys.readouterr()


@pytest.mark.asyncio
async def test_curtain_upload_image_uses_segmented_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    from PIL import Image

    rng = random.Random(3)
    pixels = [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(400)]
    image = Image.new("RGB", (20, 20))
    for index, pixel in enumerate(pixels):
        image.putpixel((index % 20, index // 20), pixel)
    path = tmp_path / "noise.png"
    image.save(path)
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        _run_args("upload", "--allow-flash-write", "--image", str(path), "--resample", "nearest",
                  "--no-play")
    ) == 0
    content = next(payload for payload in fake.payloads if payload[:2] == b"\xea\x03")
    assert len(content) > 174
    assert any(data[0] == 0x40 for _uuid, data in fake.writes)  # segmented frames were written
    assert all(len(data) <= 182 for _uuid, data in fake.writes)
    expected = curtain_c2.build_upload([curtain_c2.image_to_frame(pixels)])
    assert content == expected[2].payload
    capsys.readouterr()


@pytest.mark.asyncio
async def test_curtain_upload_animated_gif_with_max_frames_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    from PIL import Image

    colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255))
    frames = [Image.new("RGB", (20, 20), color) for color in colors]
    path = tmp_path / "anim.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        _run_args("upload", "--allow-flash-write", "--image", str(path), "--max-frames", "2",
                  "--no-play")
    ) == 0
    assert [p[:4].hex() for p in fake.payloads if p[:2] == b"\xea\x03"] == ["ea030001", "ea030002"]
    assert int.from_bytes(fake.payloads[0][23:25], "big") == 2
    captured = capsys.readouterr()
    assert "has 3 frames; keeping the first 2" in captured.err


@pytest.mark.asyncio
async def test_curtain_upload_aborts_when_ack_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    fake.silent_steps = {2}
    _patch_device(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="no reply to upload step 'address'"):
        await cli.async_main(
            _run_args("upload", "--allow-flash-write", "--solid", "255,0,0", "--ack-timeout", "0.1")
        )
    names = [payload[:2].hex() for payload in fake.payloads]
    assert names == ["ea01", "ea02"]  # nothing after the unacknowledged address step
    capsys.readouterr()


@pytest.mark.asyncio
async def test_curtain_upload_aborts_when_a_content_ack_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    fake.silent_steps = {3}
    _patch_device(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="no reply to upload step 'content 1'"):
        await cli.async_main(
            _run_args(
                "upload", "--allow-flash-write", "--solid", "255,0,0;0,0,255",
                "--ack-timeout", "0.1",
            )
        )
    # Frame 2 and the end command are not sent after an unacknowledged frame.
    assert [payload[:2].hex() for payload in fake.payloads] == ["ea01", "ea02", "ea03"]
    capsys.readouterr()


@pytest.mark.asyncio
async def test_curtain_upload_aborts_on_nonzero_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    fake.statuses = {1: 1}
    _patch_device(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="rejected upload step EA 01"):
        await cli.async_main(
            _run_args("upload", "--allow-flash-write", "--solid", "255,0,0", "--ack-timeout", "0.1")
        )
    assert [payload[:2].hex() for payload in fake.payloads] == ["ea01"]
    capsys.readouterr()


@pytest.mark.asyncio
async def test_curtain_upload_aborts_on_nonzero_content_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    fake.statuses = {3: 2}
    _patch_device(monkeypatch, fake)
    with pytest.raises(RuntimeError, match=r"rejected upload step EA 03 \(frame 1\)"):
        await cli.async_main(
            _run_args("upload", "--allow-flash-write", "--solid", "255,0,0", "--ack-timeout", "0.1")
        )
    assert not any(payload[:2] == b"\xea\x04" for payload in fake.payloads)
    capsys.readouterr()


def test_controller_side_option_parses() -> None:
    parser = build_parser()
    base = [
        "curtain", "upload", "--device", "D", "--allow-curtain-c2", "--allow-flash-write",
        "--image", "a.png",
    ]
    assert parser.parse_args(base).controller_side == "left"
    assert parser.parse_args([*base, "--controller-side", "right"]).controller_side == "right"
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--controller-side", "front"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "marker_x", "label"),
    [
        ([], 0, "left"),
        (["--controller-side", "left"], 0, "left"),
        (["--controller-side", "right"], 19, "right"),
        (["--mirror"], 19, "right"),
        (["--controller-side", "right", "--mirror"], 0, "left"),
    ],
)
async def test_curtain_upload_controller_side_maps_marker_to_controller_string(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
    extra: list[str],
    marker_x: int,
    label: str,
) -> None:
    from PIL import Image

    # Red marker on the controller-side edge column of the viewed image, top row.
    image = Image.new("RGB", (20, 20), (0, 0, 0))
    image.putpixel((marker_x, 0), (255, 0, 0))
    path = tmp_path / "marker.png"
    image.save(path)
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    assert await cli.async_main(
        _run_args("upload", "--allow-flash-write", "--image", str(path), "--resample", "nearest",
                  "--no-play", *extra)
    ) == 0
    # Buffer pixel 0 (top of the controller-side string) must be the red marker.
    pixels = [(0, 0, 0)] * 400
    pixels[0] = (255, 0, 0)
    expected = curtain_c2.build_upload([pixels])
    content = next(payload for payload in fake.payloads if payload[:2] == b"\xea\x03")
    assert content == expected[2].payload
    assert f"controller side: {label}" in capsys.readouterr().out


def test_max_frames_option_is_capped_at_the_device_limit() -> None:
    parser = build_parser()
    base = [
        "curtain", "upload", "--device", "D", "--allow-curtain-c2", "--allow-flash-write",
        "--image", "a.gif",
    ]
    assert parser.parse_args([*base, "--max-frames", "128"]).max_frames == 128
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--max-frames", "129"])


@pytest.mark.asyncio
async def test_curtain_upload_too_big_status_gives_clear_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    fake.statuses = {1: 2}
    _patch_device(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="too big for device memory"):
        await cli.async_main(
            _run_args("upload", "--allow-flash-write", "--solid", "255,0,0", "--ack-timeout", "0.1")
        )
    assert [payload[:2].hex() for payload in fake.payloads] == ["ea01"]
    capsys.readouterr()


@pytest.mark.asyncio
async def test_curtain_upload_of_128_frames_is_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    colors = ";".join("255,0,0" for _ in range(128))
    assert await cli.async_main(
        _run_args("upload", "--allow-flash-write", "--solid", colors, "--no-play")
    ) == 0
    assert int.from_bytes(fake.payloads[0][3:7], "big") == 102400
    assert sum(1 for p in fake.payloads if p[:2] == b"\xea\x03") == 128
    capsys.readouterr()


@pytest.mark.asyncio
async def test_curtain_upload_of_129_solid_frames_fails_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCurtain()
    _patch_device(monkeypatch, fake)
    colors = ";".join("255,0,0" for _ in range(129))
    with pytest.raises(ValueError, match="128 frames"):
        await cli.async_main(
            _run_args("upload", "--allow-flash-write", "--solid", colors, "--no-play")
        )
    assert fake.writes == []
