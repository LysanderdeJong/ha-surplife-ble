"""LightClient control of the IOTBT curtain (product 0xC2) with a fake device."""

from __future__ import annotations

import pytest
from curtain_fakes import FF01, FakeCurtain, _Device

from custom_components.surplife_ble.core import curtain_c2
from custom_components.surplife_ble.core.calibration import ColorCalibration
from custom_components.surplife_ble.core.client import LightClient
from custom_components.surplife_ble.core.models import ProtocolResolution

SCENE_EFFECT_10 = dict(mode=0x67, effect=0x0A, speed=0x50)
DIY_MODE_1 = dict(mode=0x74, effect=0x01, speed=0x32)


async def resolved(fake: FakeCurtain) -> LightClient:
    client = LightClient(_Device(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    await client.connect()
    resolution = await client.probe(allow_writes=True, response_timeout=0.05)
    assert resolution is not None
    assert resolution.codec == "iotbt-ea81-curtain-c2"
    assert resolution.repeatable
    fake.writes.clear()
    fake.payloads.clear()
    return client


def last_frame(fake: FakeCurtain) -> bytes:
    uuid, data = fake.writes[-1]
    assert uuid == FF01
    return data


@pytest.mark.asyncio
async def test_power_sends_the_verified_frames() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    await client.set_power(False)
    data = last_frame(fake)
    assert data[0] == 0
    assert data[2:] == bytes.fromhex("8000000d0e0b3b240000000000000032000091")
    await client.set_power(True)
    assert last_frame(fake)[2:] == bytes.fromhex("8000000d0e0b3b230000000000000032000090")
    assert client.state is not None and client.state.power is True and client.state.assumed


@pytest.mark.asyncio
async def test_color_sends_3b_a1_and_remembers_the_assumed_color() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    await client.set_rgb(0, 0, 255, brightness=50)
    assert last_frame(fake)[2:] == bytes.fromhex("8000000d0e0b3ba178643200000000000000ea")
    assert client.curtain_rgb == (0, 0, 255)
    assert client.curtain_percent == 50
    assert client.state is not None
    assert (client.state.rgb, client.state.assumed, client.state.mode) == (
        (0, 0, 255),
        True,
        0x66,
    )


@pytest.mark.asyncio
async def test_brightness_resends_the_color_never_the_standalone_command() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    await client.set_rgb(0, 0, 255, brightness=100)
    await client.set_brightness(10)
    assert last_frame(fake)[2:] == bytes.fromhex("8000000d0e0b3ba178640a00000000000000c2")
    assert all(payload[:2] != b"\x3b\x01" for payload in fake.payloads)


@pytest.mark.asyncio
async def test_brightness_without_a_known_color_uses_white() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    await client.set_brightness(100)
    assert fake.payloads[-1] == curtain_c2.color_rgb(255, 255, 255, 100).payload


@pytest.mark.asyncio
async def test_effect_frame_uses_command_family_0a() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    await client.set_effect(10, speed=80, brightness=100)
    data = last_frame(fake)
    assert data[2:] == bytes.fromhex("80000006070ae002000a5064")
    assert client.state is not None
    assert (client.state.mode, client.state.effect, client.state.speed) == (0x67, 10, 80)


@pytest.mark.asyncio
async def test_play_animation_frame() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    await client.play_animation(1, 50, 100)
    assert last_frame(fake)[2:] == bytes.fromhex("80000006070ae20901326400")
    await client.play_animation(2, 50, 100)  # hold frame 1
    assert fake.payloads[-1] == bytes.fromhex("e20902326400")
    assert client.state is not None
    assert (client.state.mode, client.state.play_mode) == (0x74, 2)


@pytest.mark.asyncio
async def test_brightness_in_effect_mode_resends_the_effect() -> None:
    fake = FakeCurtain()
    fake.mode, fake.effect, fake.speed = 0x67, 0x0A, 0x50
    client = await resolved(fake)
    state = await client.query_state()
    assert state is not None and state.mode == 0x67
    await client.set_brightness(30)
    assert fake.payloads[-1] == bytes.fromhex("e002000a501e")


@pytest.mark.asyncio
async def test_brightness_in_animation_mode_resends_play_with_the_same_mode() -> None:
    fake = FakeCurtain()
    fake.mode, fake.effect, fake.speed = 0x74, 0x02, 0x32
    client = await resolved(fake)
    await client.query_state()
    await client.set_brightness(30)
    assert fake.payloads[-1] == bytes.fromhex("e20902321e00")


@pytest.mark.asyncio
async def test_commands_turn_the_curtain_on_first_when_it_is_off() -> None:
    fake = FakeCurtain()
    fake.power = False
    client = await resolved(fake)
    state = await client.query_state()
    assert state is not None and state.power is False
    await client.set_rgb(255, 0, 0, brightness=100)
    assert fake.payloads[-2] == curtain_c2.power(True).payload
    assert fake.payloads[-1] == curtain_c2.color_rgb(255, 0, 0, 100).payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (dict(power=True, mode=0x62, effect=1, speed=0x64), (True, 0x62, None, None, None)),
        (dict(power=True, **SCENE_EFFECT_10), (True, 0x67, 10, None, 0x50)),
        (dict(power=True, **DIY_MODE_1), (True, 0x74, None, 1, 0x32)),
        (dict(power=False, mode=0x62, effect=1, speed=0x64), (False, 0x62, None, None, None)),
    ],
)
async def test_poll_maps_ea81_mode_effect_and_speed(
    fields: dict[str, int | bool], expected: tuple[bool, int, int | None, int | None, int | None]
) -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    for name, value in fields.items():
        setattr(fake, name, value)
    state = await client.query_state()
    assert state is not None
    assert (state.power, state.mode, state.effect, state.play_mode, state.speed) == expected
    assert state.rgb is None and state.brightness is None  # never reported by the device


@pytest.mark.asyncio
async def test_white_is_not_available() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    with pytest.raises(RuntimeError, match="RGB-only"):
        await client.set_white(50)
    assert fake.writes == []


@pytest.mark.asyncio
async def test_upload_curtain_animation_uploads_then_plays() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    frames = [curtain_c2.solid_frame((255, 0, 0)), curtain_c2.solid_frame((0, 0, 255))]
    await client.upload_curtain_animation(frames, mode=1, speed=50, brightness=100)
    assert [payload[:2].hex() for payload in fake.payloads] == [
        "ea01",
        "ea02",
        "ea03",
        "ea03",
        "ea04",
        "e209",
    ]
    assert fake.payloads[-1] == bytes.fromhex("e20901326400")
    assert {uuid for uuid, _ in fake.writes} == {FF01}


@pytest.mark.asyncio
async def test_upload_status_2_is_reported_as_too_big() -> None:
    fake = FakeCurtain()
    fake.statuses = {1: 2}
    client = await resolved(fake)
    with pytest.raises(curtain_c2.CurtainUploadError, match="too big for device memory"):
        await client.upload_curtain_animation([curtain_c2.solid_frame((1, 2, 3))])
    assert [payload[:2].hex() for payload in fake.payloads] == ["ea01"]


@pytest.mark.asyncio
async def test_upload_and_effects_need_a_resolved_curtain() -> None:
    fake = FakeCurtain()
    client = LightClient(_Device(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    await client.connect()
    for call in (
        client.set_effect(1),
        client.play_animation(),
        client.upload_curtain_animation([curtain_c2.solid_frame((1, 2, 3))]),
    ):
        with pytest.raises(RuntimeError, match="not an IOTBT curtain"):
            await call
    assert fake.writes == []


@pytest.mark.asyncio
async def test_other_unresolved_codecs_stay_locked() -> None:
    client = LightClient(_Device(), client_factory=lambda _device: FakeCurtain())  # type: ignore[arg-type]
    client.resolution = ProtocolResolution(
        frame_family="extended-ea81",
        codec="unresolved-ea81",
        confidence=0.5,
        repeatable=True,
    )
    client._probe_frame = object()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="locked"):
        await client.set_power(True)
    with pytest.raises(RuntimeError, match="locked"):
        await client.set_rgb(1, 2, 3)


@pytest.mark.asyncio
async def test_curtain_codec_routes_control_through_curtain_c2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The curtain codec is unlocked: control uses the curtain_c2 builders, not the lock."""
    fake = FakeCurtain()
    client = await resolved(fake)
    resolution, _probe = client._require_control_resolution()  # does not raise any more
    assert resolution.codec == "iotbt-ea81-curtain-c2"

    calls: list[str] = []
    for name in ("power", "color_rgb"):
        original = getattr(curtain_c2, name)

        def spy(
            *args: object, _name: str = name, _original: object = original, **kwargs: object
        ) -> object:
            calls.append(_name)
            return _original(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(curtain_c2, name, spy)
    await client.set_power(True)
    await client.set_rgb(1, 2, 3, brightness=40)
    await client.set_brightness(20)
    assert calls == ["power", "color_rgb", "color_rgb"]


@pytest.mark.asyncio
async def test_calibration_applies_to_color_and_brightness_commands() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    client.curtain_calibration = ColorCalibration(green_gain=0.5)
    await client.set_rgb(0, 255, 255, brightness=100)
    # (0, 255, 255) becomes (0, 128, 255): hue 210 instead of 180.
    assert last_frame(fake)[2:] == bytes.fromhex("8000000d0e0b3ba1696464000000000000000d")
    # The assumed state keeps the color Home Assistant asked for.
    assert client.curtain_rgb == (0, 255, 255)
    assert client.state is not None and client.state.rgb == (0, 255, 255)
    await client.set_brightness(50)
    assert fake.payloads[-1] == curtain_c2.color_rgb(
        0, 255, 255, 50, calibration=client.curtain_calibration
    ).payload
    assert fake.payloads[-1].hex().startswith("3ba169643200")


@pytest.mark.asyncio
async def test_default_calibration_is_the_identity() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    assert client.curtain_calibration.is_identity
    await client.set_rgb(0, 255, 255, brightness=100)
    assert last_frame(fake)[2:] == bytes.fromhex("8000000d0e0b3ba15a646400000000000000fe")


@pytest.mark.asyncio
async def test_calibration_applies_to_uploads_but_not_to_effects_or_playback() -> None:
    fake = FakeCurtain()
    client = await resolved(fake)
    calibration = ColorCalibration(gamma=2.0)
    client.curtain_calibration = calibration
    frames = [[(255, 120, 0)] * 400]
    await client.upload_curtain_animation(frames, mode=1, speed=50, brightness=100)
    expected = curtain_c2.build_upload(frames, calibration)
    uploaded = [payload for payload in fake.payloads if payload[:2] == b"\xea\x03"]
    assert uploaded == [expected[2].payload]
    assert uploaded[0] != curtain_c2.build_upload(frames)[2].payload
    await client.set_effect(10, 80, 100)
    assert fake.payloads[-1] == bytes.fromhex("e002000a5064")
    await client.play_animation(1, 50, 100)
    assert fake.payloads[-1] == bytes.fromhex("e20901326400")
