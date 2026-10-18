"""Flow control of the curtain upload: every frame is acked before the next one is sent."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from curtain_fakes import FF01, FakeCurtain, _Device

from custom_components.surplife_ble.core import curtain_c2
from custom_components.surplife_ble.core.client import LightClient

RED = (255, 0, 0)
BLUE = (0, 0, 255)


class GatedCurtain(FakeCurtain):
    """A curtain that can hold back or change the replies to content frames."""

    def __init__(self) -> None:
        super().__init__()
        self.hold_content = False
        self.held: dict[int, bytes] = {}
        self.index_offset = 0  # added to the index of every content reply

    def _reply(self, body: bytes) -> None:
        if body[:2] == b"\xea\x03":
            index = int.from_bytes(body[2:4], "big")
            if self.hold_content:
                self.held[index] = body
                return
            if self.index_offset:
                shifted = (index + self.index_offset).to_bytes(2, "big")
                body = body[:2] + shifted + body[4:]
        super()._reply(body)

    def release(self, index: int) -> None:
        super()._reply(self.held.pop(index))

    def content_writes(self) -> list[int]:
        return [
            int.from_bytes(payload[2:4], "big")
            for payload in self.payloads
            if payload[:2] == b"\xea\x03"
        ]

    def sent(self) -> list[str]:
        return [payload[:2].hex() for payload in self.payloads]


async def connected(fake: FakeCurtain) -> LightClient:
    client = LightClient(_Device(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    await client.connect()
    return client


async def until(condition: Any, limit: float = 2.0) -> None:
    """Wait (yielding to the event loop) until ``condition()`` is true."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not condition():
        assert loop.time() < deadline, "timed out waiting for the condition"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_next_frame_is_not_sent_before_the_previous_one_is_acked() -> None:
    fake = GatedCurtain()
    fake.hold_content = True
    client = await connected(fake)
    steps = curtain_c2.build_upload([curtain_c2.solid_frame(c) for c in (RED, BLUE, RED)])
    task = asyncio.create_task(client.upload_steps(steps, ack_timeout=5.0))

    await until(lambda: fake.content_writes() == [1])
    await asyncio.sleep(0.1)  # plenty of time for a sender that does not wait
    assert fake.content_writes() == [1]
    assert fake.sent() == ["ea01", "ea02", "ea03"]
    assert not task.done()

    fake.release(1)
    await until(lambda: fake.content_writes() == [1, 2])
    await asyncio.sleep(0.1)
    assert fake.content_writes() == [1, 2]

    fake.release(2)
    await until(lambda: fake.content_writes() == [1, 2, 3])
    await asyncio.sleep(0.1)
    assert fake.sent() == ["ea01", "ea02", "ea03", "ea03", "ea03"]  # no end command yet
    assert not task.done()

    fake.release(3)
    await asyncio.wait_for(task, 2.0)
    assert fake.sent() == ["ea01", "ea02", "ea03", "ea03", "ea03", "ea04"]
    assert {uuid for uuid, _ in fake.writes} == {FF01}


@pytest.mark.asyncio
async def test_a_missing_content_ack_raises_with_the_frame_index() -> None:
    fake = GatedCurtain()
    client = await connected(fake)
    frames = [curtain_c2.solid_frame(RED)] * 5
    original = fake._reply

    def drop_frame_3(body: bytes) -> None:
        if body[:4] == b"\xea\x03\x00\x03":
            return
        original(body)

    fake._reply = drop_frame_3  # type: ignore[method-assign]
    with pytest.raises(curtain_c2.CurtainUploadError, match="no reply to upload step 'content 3'"):
        await client.upload_frames(frames, ack_timeout=0.1)
    assert fake.content_writes() == [1, 2, 3]  # frames 4 and 5 and the end are never sent
    assert "ea04" not in fake.sent()


@pytest.mark.asyncio
async def test_a_nonzero_content_status_stops_the_upload() -> None:
    fake = GatedCurtain()
    fake.statuses = {3: 2}
    client = await connected(fake)
    frames = [curtain_c2.solid_frame(RED)] * 3
    with pytest.raises(curtain_c2.CurtainUploadError, match=r"EA 03 \(frame 1\).*too big"):
        await client.upload_frames(frames, ack_timeout=0.5)
    assert fake.content_writes() == [1]
    fake.statuses = {3: 5}
    with pytest.raises(curtain_c2.CurtainUploadError, match=r"EA 03 \(frame 1\): status 0x05"):
        await client.upload_frames(frames, ack_timeout=0.5)


@pytest.mark.asyncio
async def test_a_reply_for_another_index_is_not_the_ack() -> None:
    fake = GatedCurtain()
    fake.index_offset = 1  # the device answers frame N with a reply for frame N + 1
    client = await connected(fake)
    lines: list[str] = []
    with pytest.raises(curtain_c2.CurtainUploadError, match="no reply to upload step 'content 1'"):
        await client.upload_frames(
            [curtain_c2.solid_frame(RED)] * 2, ack_timeout=0.1, progress=lines.append
        )
    assert fake.content_writes() == [1]
    assert any(
        "ignoring reply EA 03 for frame 2 while waiting for 'content 1'" in line for line in lines
    )


@pytest.mark.asyncio
async def test_a_stray_reply_is_ignored_while_waiting_for_the_right_one() -> None:
    fake = GatedCurtain()
    fake.hold_content = True
    client = await connected(fake)
    lines: list[str] = []
    steps = curtain_c2.build_upload([curtain_c2.solid_frame(RED)])
    task = asyncio.create_task(client.upload_steps(steps, ack_timeout=5.0, progress=lines.append))
    await until(lambda: fake.content_writes() == [1])
    super(GatedCurtain, fake)._reply(bytes((0xEA, 0x03, 0x00, 0x07, 0x00)))  # frame 7, status 0
    fake._push(ctrl=0x04, cmd=0x15)  # an unrelated state notification
    await asyncio.sleep(0.05)
    assert not task.done()
    fake.release(1)
    await asyncio.wait_for(task, 2.0)
    assert any("ignoring reply EA 03 for frame 7" in line for line in lines)
    assert any(line == "  reply: EA 03 status 0x00" for line in lines)


@pytest.mark.asyncio
async def test_a_nonzero_status_for_another_frame_is_still_an_error() -> None:
    fake = GatedCurtain()
    fake.hold_content = True
    client = await connected(fake)
    steps = curtain_c2.build_upload([curtain_c2.solid_frame(RED)])
    task = asyncio.create_task(client.upload_steps(steps, ack_timeout=5.0))
    await until(lambda: fake.content_writes() == [1])
    super(GatedCurtain, fake)._reply(bytes((0xEA, 0x03, 0x00, 0x09, 0x01)))
    with pytest.raises(curtain_c2.CurtainUploadError, match=r"EA 03 \(frame 9\): status 0x01"):
        await asyncio.wait_for(task, 2.0)


@pytest.mark.asyncio
async def test_the_frame_limit_is_still_checked_before_anything_is_sent() -> None:
    fake = GatedCurtain()
    client = await connected(fake)
    with pytest.raises(ValueError, match="128 frames"):
        await client.upload_frames([curtain_c2.solid_frame(RED)] * 129)
    assert fake.writes == []


@pytest.mark.asyncio
async def test_a_full_upload_with_acks_for_every_frame_still_works() -> None:
    fake = GatedCurtain()
    client = await connected(fake)
    lines: list[str] = []
    await client.upload_frames(
        [curtain_c2.solid_frame(RED)] * 128, ack_timeout=1.0, progress=lines.append
    )
    assert fake.content_writes() == list(range(1, 129))
    assert fake.sent()[-1] == "ea04"
    assert sum(1 for line in lines if line == "  reply: EA 03 status 0x00") == 128
