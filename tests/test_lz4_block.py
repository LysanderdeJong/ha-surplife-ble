"""The built-in LZ4 block codec."""

from __future__ import annotations

import importlib
import random
import sys

import pytest

from custom_components.surplife_ble.core import curtain_c2, lz4_block

RNG = random.Random(1234)


def _frames() -> dict[str, bytes]:
    gradient = [
        (x * 12 % 256, y * 12 % 256, (x + y) * 6 % 256) for y in range(20) for x in range(20)
    ]
    noisy = [tuple(RNG.randrange(256) for _ in range(3)) for _ in range(400)]
    two = [(255, 0, 0) if (i // 3) % 2 else (0, 0, 255) for i in range(400)]
    return {
        "red": curtain_c2.frame_blob([(255, 0, 0)] * 400),
        "blue": curtain_c2.frame_blob([(0, 0, 255)] * 400),
        "black": curtain_c2.frame_blob([(0, 0, 0)] * 400),
        "stripes": curtain_c2.frame_blob(two),
        "gradient": curtain_c2.frame_blob(gradient),
        "noise": curtain_c2.frame_blob(noisy),
    }


def _samples() -> list[bytes]:
    samples = [b"", b"a", b"\x00" * 800, bytes.fromhex("01ff") * 400, b"ab" * 400, b"abc" * 300]
    samples += [bytes(RNG.randrange(256) for _ in range(n)) for n in range(65)]
    samples += [bytes(RNG.randrange(256) for _ in range(n)) for n in (200, 800, 2000)]
    samples += [bytes(RNG.randrange(4) for _ in range(n)) for n in (13, 17, 100, 800, 2000)]
    samples += [b"x" * n for n in range(40)]
    samples += list(_frames().values())
    return samples


def _check_end_rules(block: bytes, size: int) -> None:
    """Walk the sequences and check the last-literals and last-match rules."""
    pos = 0
    out_len = 0
    while True:
        token = block[pos]
        pos += 1
        lit = token >> 4
        if lit == 15:
            while block[pos] == 255:
                lit += 255
                pos += 1
            lit += block[pos]
            pos += 1
        pos += lit
        out_len += lit
        if pos == len(block):
            assert lit >= min(size, 5) or out_len == size
            return
        pos += 2
        ml = token & 15
        if ml == 15:
            while block[pos] == 255:
                ml += 255
                pos += 1
            ml += block[pos]
            pos += 1
        start = out_len
        out_len += ml + 4
        assert start <= size - 12
        assert out_len <= size - 5


@pytest.mark.parametrize("data", _samples(), ids=lambda d: f"len{len(d)}")
def test_round_trip_and_end_rules(data: bytes) -> None:
    block = lz4_block.compress(data)
    assert lz4_block.decompress(block, len(data)) == data
    _check_end_rules(block, len(data))


def test_edge_inputs() -> None:
    assert lz4_block.compress(b"") == b"\x00"
    assert lz4_block.compress(b"abc") == b"\x30abc"
    assert lz4_block.compress(b"a" * 12) == b"\xc0" + b"a" * 12  # < 13 bytes is literal-only
    assert len(lz4_block.compress(b"ab" * 400)) < 30


def test_solid_frame_is_tiny() -> None:
    for name in ("red", "blue", "black"):
        assert len(lz4_block.compress(_frames()[name])) <= 16


def test_offsets_stay_within_64k() -> None:
    chunk = bytes(RNG.randrange(256) for _ in range(70000))
    data = chunk + chunk[:500]
    assert lz4_block.decompress(lz4_block.compress(data), len(data)) == data


@pytest.mark.parametrize(
    ("block", "size"),
    [
        (b"", 0),
        (b"\x10", 1),  # literal missing
        (b"\x10a\x01", 5),  # truncated offset
        (b"\x10a\x00\x00", 5),  # zero offset
        (b"\x10a\x02\x00", 5),  # offset beyond output
        (b"\xf0", 15),  # truncated literal length
        (b"\x00", 1),  # too short
        (b"\x10a", 0),  # too long
        (b"\x1fa\x01\x00", 10),  # truncated match length
    ],
)
def test_decompress_rejects_malformed(block: bytes, size: int) -> None:
    with pytest.raises(ValueError):
        lz4_block.decompress(block, size)


def test_matches_python_lz4() -> None:
    lz4b = pytest.importorskip("lz4.block")
    rows = []
    for name, data in _frames().items():
        ours = lz4_block.compress(data)
        theirs = lz4b.compress(data, store_size=False)
        assert lz4b.decompress(ours, uncompressed_size=len(data)) == data
        assert lz4_block.decompress(theirs, len(data)) == data
        assert len(ours) <= max(16, len(theirs) * 1.5)
        rows.append(f"{name}: ours={len(ours)} python-lz4={len(theirs)}")
    print("\n".join(rows))
    for data in _samples():
        assert lz4b.decompress(lz4_block.compress(data), uncompressed_size=len(data)) == data


def test_upload_needs_no_lz4_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "lz4", None)
    monkeypatch.setitem(sys.modules, "lz4.block", None)
    module = importlib.reload(lz4_block)
    try:
        reloaded = importlib.reload(curtain_c2)
        steps = reloaded.build_upload([[(255, 0, 0)] * 400, [(0, 0, 255)] * 400])
        assert len(steps) == 5
        assert steps[-1].name == "end"
        assert module.decompress(steps[2].payload[4:], 800) == reloaded.frame_blob(
            [(255, 0, 0)] * 400
        )
    finally:
        monkeypatch.undo()
        importlib.reload(lz4_block)
        importlib.reload(curtain_c2)
