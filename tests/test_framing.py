import hashlib

import pytest

from custom_components.surplife_ble.core.framing import (
    NotificationReassembler,
    SequenceCounter,
    additive_checksum,
    candidate_payloads,
    checksum_valid,
    unwrap_magichome,
    with_checksum,
    wrap_magichome,
    wrap_magichome_segments,
)


def test_checksum_round_trip() -> None:
    payload = bytes.fromhex("818a8b")
    assert additive_checksum(payload) == 0x96
    assert with_checksum(payload) == bytes.fromhex("818a8b96")
    assert checksum_valid(bytes.fromhex("818a8b96"))
    assert not checksum_valid(bytes.fromhex("818a8b97"))
    assert not checksum_valid(b"")


def test_magichome_wrappers_are_explicit() -> None:
    inner = bytes.fromhex("ea818a8b")
    standard = wrap_magichome(inner, 3)
    plus_two = wrap_magichome(inner, 4, length_bias=2)
    family_0b = wrap_magichome(inner, 5, command_family=0x0B)

    assert standard.hex() == "000380000004050aea818a8b"
    assert plus_two.hex() == "000480000006050aea818a8b"
    assert family_0b.hex() == "000580000004050bea818a8b"
    assert unwrap_magichome(standard) == (inner, "magichome2:length_delta=0")
    assert unwrap_magichome(plus_two) == (inner, "magichome2:length_delta=2")
    assert any(payload == inner for payload, _description in candidate_payloads(standard))


def _reference_frames(payload: bytes, cmd: int, seq: int, maxlen: int) -> list[bytes]:
    """Independent copy of frames() from tools/zg_send.py (hardware-tested tool)."""
    n = len(payload)
    if n <= maxlen - 8:
        return [
            bytes([0x00, seq & 0xFF, 0x80, 0x00, n >> 8, n & 0xFF, (n + 1) & 0xFF, cmd]) + payload
        ]
    first = maxlen - 8
    out = [
        bytes([0x40, seq & 0xFF, 0x00, 0x00, n >> 8, n & 0xFF, (maxlen - 7) & 0xFF, cmd])
        + payload[:first]
    ]
    pos, idx = first, 1
    step = maxlen - 5
    while pos < n:
        chunk = payload[pos : pos + step]
        pos += len(chunk)
        sidx = idx | (0x8000 if pos >= n else 0)
        out.append(bytes([0x40, seq & 0xFF, sidx >> 8, sidx & 0xFF, len(chunk)]) + chunk)
        idx += 1
    return out


# FrameInfo-style DIY upload: E2 11 01 01 00 01 00 + 400 pixels of 01 FF (807 bytes).
DIY_PAYLOAD = bytes.fromhex("e2110101000100") + bytes.fromhex("01ff") * 400


def test_segments_807_byte_payload_matches_reference_vector() -> None:
    frames = wrap_magichome_segments(DIY_PAYLOAD, 1, command_family=0x0A, max_len=182)
    assert len(DIY_PAYLOAD) == 807
    assert [len(frame) for frame in frames] == [182, 182, 182, 182, 107]
    assert [frame[:8].hex() for frame in frames] == [
        "400100000327af0a",
        "40010001b1ff01ff",
        "40010002b101ff01",
        "40010003b1ff01ff",
        "400180046601ff01",
    ]
    assert frames == _reference_frames(DIY_PAYLOAD, 0x0A, 1, 182)
    assert hashlib.sha256(b"".join(frames)).hexdigest() == (
        "ef6e394d4bc7a0c13b37d1f47f82178e4dedfdc4d721997115106a481505b463"
    )
    # The segments reassemble to the original payload.
    assert frames[0][8:] + b"".join(frame[5:] for frame in frames[1:]) == DIY_PAYLOAD


def test_short_payload_is_one_frame_identical_to_wrap_magichome() -> None:
    payload = bytes.fromhex("3b230000000000000032000090")
    assert wrap_magichome_segments(payload, 2, command_family=0x0B) == [
        wrap_magichome(payload, 2, command_family=0x0B)
    ]
    assert wrap_magichome_segments(b"", 9) == [wrap_magichome(b"", 9)]


def test_segment_boundary_at_max_len_minus_8() -> None:
    at_limit = bytes(range(174))
    frames = wrap_magichome_segments(at_limit, 5, command_family=0x0A, max_len=182)
    assert frames == [wrap_magichome(at_limit, 5)]
    assert len(frames[0]) == 182


def test_segment_boundary_at_max_len_minus_7() -> None:
    payload = bytes(range(175))
    frames = wrap_magichome_segments(payload, 5, command_family=0x0A, max_len=182)
    assert len(frames) == 2
    assert frames[0] == bytes.fromhex("4005000000afaf0a") + payload[:174]
    assert len(frames[0]) == 182
    assert frames[1] == bytes.fromhex("4005800101") + payload[174:]
    assert frames == _reference_frames(payload, 0x0A, 5, 182)


def test_segment_chunk_boundaries() -> None:
    # 174 first bytes + 177 per later segment.
    exactly_two = bytes(174 + 177)
    frames = wrap_magichome_segments(exactly_two, 1, max_len=182)
    assert [len(frame) for frame in frames] == [182, 182]
    assert frames[1][2:5] == bytes.fromhex("8001b1")
    three = bytes(174 + 177 + 1)
    frames = wrap_magichome_segments(three, 1, max_len=182)
    assert [len(frame) for frame in frames] == [182, 182, 6]
    assert frames[1][2:4] == bytes.fromhex("0001")
    assert frames[2][2:4] == bytes.fromhex("8002")
    assert frames == _reference_frames(three, 0x0A, 1, 182)


def test_segments_other_max_len_matches_reference() -> None:
    payload = bytes(range(256)) * 3
    for max_len in (20, 100, 182, 255):
        assert wrap_magichome_segments(payload, 0xFF, command_family=0x0B, max_len=max_len) == (
            _reference_frames(payload, 0x0B, 0xFF, max_len)
        )


def test_segments_reject_bad_arguments() -> None:
    with pytest.raises(ValueError):
        wrap_magichome_segments(b"\x00", 1, max_len=8)
    with pytest.raises(ValueError):
        wrap_magichome_segments(b"\x00", 1, max_len=256)
    with pytest.raises(ValueError):
        wrap_magichome_segments(b"\x00", 256)
    with pytest.raises(ValueError):
        wrap_magichome_segments(b"\x00", 1, command_family=256)
    with pytest.raises(ValueError):
        wrap_magichome_segments(bytes(0x10000), 1)


def test_sequence_rollover() -> None:
    sequence = SequenceCounter(0xFE)
    assert sequence.next() == 0xFF
    assert sequence.next() == 0
    assert sequence.next() == 1


def test_wrapped_notification_reassembly() -> None:
    inner = bytes.fromhex("ea810100b60924660164f000000000640500640000002002010003")
    packet = wrap_magichome(inner, 7)
    reassembler = NotificationReassembler()
    assert reassembler.feed(packet[:11]) == []
    assert reassembler.feed(packet[11:]) == [packet]


def test_json_wrapped_iotbt_payload_is_extracted() -> None:
    packet = bytes.fromhex(
        "041e8000004d4e0a7b22636f6465223a302c227061796c6f6164223a"
        "2245413831303130304141303932333235364635304630303036343634"
        "3030363430353030314446464646464646463031303030303030227d"
    )
    expected = bytes.fromhex("ea810100aa0923256f50f0006464006405001dffffffff01000000")
    assert any(payload == expected for payload, _description in candidate_payloads(packet))


def test_extended_notification_reassembly() -> None:
    packet = bytes.fromhex("ea810100b60924660164f00000000064050064000000200201000003")
    reassembler = NotificationReassembler()
    assert reassembler.feed(packet[:20]) == []
    assert reassembler.feed(packet[20:]) == [packet]


def test_unknown_notifications_are_preserved() -> None:
    reassembler = NotificationReassembler()
    assert reassembler.feed(bytes.fromhex("010203")) == [bytes.fromhex("010203")]
