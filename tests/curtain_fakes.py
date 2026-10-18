"""Fake IOTBT437 curtain (product 0xC2) for CLI and Home Assistant tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from custom_components.surplife_ble.core.framing import wrap_magichome

FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"
FF11 = "0000ff11-0000-1000-8000-00805f9b34fb"
FF22 = "0000ff22-0000-1000-8000-00805f9b34fb"


@dataclass
class _Char:
    uuid: str
    handle: int
    properties: list[str]
    descriptors: list[Any] = field(default_factory=list)


@dataclass
class _Service:
    uuid: str
    handle: int
    characteristics: list[_Char]


@dataclass
class _Device:
    address: str = "CURTAIN"
    name: str = "IOTBT437"


class _Backend:
    def __init__(self, owner: FakeCurtain) -> None:
        self.owner = owner

    async def _acquire_mtu(self) -> None:
        self.owner.mtu_acquired = True
        self.owner.mtu_size = 512


class FakeCurtain:
    """Fake IOTBT437: answers EA 81 queries and pushes state after some commands."""

    def __init__(self, *, push_after_commands: bool = True) -> None:
        self.is_connected = False
        self.mtu_size = 23
        self.mtu_acquired = False
        self._backend = _Backend(self)
        self.push_after_commands = push_after_commands
        self.power = True
        self.mode = 0x62
        self.effect = 0x01
        self.speed = 0x64
        self.services = [
            _Service(
                "0000ffff-0000-1000-8000-00805f9b34fb",
                1,
                [
                    _Char(FF02, 0x14, ["read", "notify"]),
                    _Char(FF01, 0x17, ["write-without-response"]),
                ],
            ),
            _Service(
                "0000fe00-0000-1000-8000-00805f9b34fb",
                0x30,
                [_Char(FF22, 0x31, ["notify"]), _Char(FF11, 0x33, ["write-without-response"])],
            ),
        ]
        self.callbacks: dict[str, Any] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.notify_order: list[str] = []
        self.payloads: list[bytes] = []
        self.silent_steps: set[int] = set()
        self.statuses: dict[int, int] = {}
        self._segments = bytearray()
        self._segment_total = 0

    def state_bytes(self) -> bytes:
        return bytes.fromhex(
            "ea810100c203"
            + ("23" if self.power else "24")
            + f"{self.mode:02x}{self.effect:02x}{self.speed:02x}"
            + "f00000000000010190000001020300"
        )

    def _push(self, ctrl: int = 0x00, cmd: int = 0x0A) -> None:
        body = self.state_bytes()
        frame = wrap_magichome(body, 9, command_family=cmd)
        packet = bytes((ctrl,)) + frame[1:]
        self.callbacks[FF02](_Char(FF02, 0x14, []), bytearray(packet))

    async def connect(self) -> bool:
        self.is_connected = True
        return True

    async def disconnect(self) -> bool:
        self.is_connected = False
        return True

    async def start_notify(self, uuid: str, callback: Any) -> None:
        self.notify_order.append(uuid)
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid: str) -> None:
        self.callbacks.pop(uuid, None)

    async def read_gatt_char(self, uuid: str) -> bytearray:
        return bytearray()

    def _reply(self, body: bytes) -> None:
        frame = wrap_magichome(body, 9, command_family=0x15)
        self.callbacks[FF02](_Char(FF02, 0x14, []), bytearray(bytes((0x04,)) + frame[1:]))

    def _receive(self, data: bytes) -> bytes | None:
        """Return a complete inner payload, reassembling segmented writes."""
        if data[0] == 0x40 and data[2:4] == b"\x00\x00":  # first segment
            total = int.from_bytes(data[4:6], "big")
            self._segments = bytearray(data[8:])
            self._segment_total = total
        elif data[0] == 0x40:
            self._segments += data[5:]
        else:
            return data[8:]
        if len(self._segments) >= self._segment_total:
            return bytes(self._segments)
        return None

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        assert FF02 in self.callbacks, "must subscribe to FF02 before writing"
        self.writes.append((uuid, bytes(data)))
        inner = self._receive(bytes(data))
        if inner is None:
            return
        self.payloads.append(inner)
        if inner == bytes.fromhex("ea818a8b"):
            self._push(ctrl=0x00)
        elif inner == bytes.fromhex("818a8b96") and bytes(data)[2:4] == b"\x80\x00":
            self._push(ctrl=0x00)  # the classic MagicHome2 probe query is answered too
        elif inner[:2] == b"\x3b\x23" or inner[:2] == b"\x3b\x24":
            self.power = inner[1] == 0x23
            if self.push_after_commands:
                self._push(ctrl=0x04, cmd=0x15)
        elif inner[:2] == b"\x3b\xa1":
            self.mode, self.effect, self.speed = 0x66, 0x6E, 0x64  # color is not reported
        elif inner[:2] == b"\xe0\x02":
            self.mode, self.effect, self.speed = 0x67, inner[3], inner[4]
            self._push(ctrl=0x04, cmd=0x16)
        elif inner[:2] in (b"\xea\x01", b"\xea\x02", b"\xea\x03", b"\xea\x04"):
            step = inner[1]
            if step in self.silent_steps:
                return
            status = self.statuses.get(step, 0)
            if step == 3:
                self._reply(bytes((0xEA, 0x03)) + inner[2:4] + bytes((status,)))
            else:
                self._reply(bytes((0xEA, step, status)))
        elif inner[:2] == b"\xe2\x09":
            self.mode, self.effect, self.speed = 0x74, inner[2], inner[3]
            self._push(ctrl=0x04, cmd=0x16)
