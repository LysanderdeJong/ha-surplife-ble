"""Pure-Python LZ4 block compressor and decompressor.

The IOTBT curtain takes every animation frame as a raw LZ4 block. The python-lz4 package
would do that, but it has no wheel for every platform Home Assistant runs on (for example
musl with a new CPython), so installing it can fail. Frames are only 800 bytes, so this
small greedy encoder is fast enough and keeps the integration free of native dependencies.

Format: https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md
"""

from __future__ import annotations

_MIN_MATCH = 4
_LAST_LITERALS = 5  # the last 5 bytes are always literals
_MF_LIMIT = 12  # the last match must start at least 12 bytes before the end
_MAX_OFFSET = 65535


def _length_extension(out: bytearray, value: int) -> None:
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)


def _emit(out: bytearray, literals: bytes, match_len: int, offset: int) -> None:
    """Append one sequence. ``match_len`` 0 means a final literal-only sequence."""
    lit_len = len(literals)
    token = (min(lit_len, 15) << 4) | (min(match_len - _MIN_MATCH, 15) if match_len else 0)
    out.append(token)
    if lit_len >= 15:
        _length_extension(out, lit_len - 15)
    out += literals
    if match_len:
        out += offset.to_bytes(2, "little")
        if match_len - _MIN_MATCH >= 15:
            _length_extension(out, match_len - _MIN_MATCH - 15)


def compress(data: bytes) -> bytes:
    """Compress ``data`` into a raw LZ4 block (no frame header, no size prefix)."""
    data = bytes(data)
    size = len(data)
    out = bytearray()
    if size < _MF_LIMIT + 1:
        _emit(out, data, 0, 0)
        return bytes(out)
    match_start_limit = size - _MF_LIMIT  # last allowed match start (inclusive)
    match_end_limit = size - _LAST_LITERALS  # matches may not extend past this
    table: dict[bytes, int] = {}
    anchor = 0
    pos = 0
    while pos <= match_start_limit:
        key = data[pos : pos + 4]
        candidate = table.get(key, -1)
        table[key] = pos
        if candidate < 0 or pos - candidate > _MAX_OFFSET:
            pos += 1
            continue
        end = pos + 4
        cand = candidate + 4
        while end < match_end_limit and data[end] == data[cand]:
            end += 1
            cand += 1
        _emit(out, data[anchor:pos], end - pos, pos - candidate)
        pos = anchor = end
    _emit(out, data[anchor:], 0, 0)
    return bytes(out)


def decompress(block: bytes, size: int) -> bytes:
    """Decompress a raw LZ4 block that must expand to exactly ``size`` bytes."""
    out = bytearray()
    pos = 0
    total = len(block)
    while True:
        if pos >= total:
            raise ValueError("truncated LZ4 block: missing token")
        token = block[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if pos >= total:
                    raise ValueError("truncated LZ4 block: literal length")
                extra = block[pos]
                pos += 1
                lit_len += extra
                if extra != 255:
                    break
        if pos + lit_len > total:
            raise ValueError("truncated LZ4 block: literals")
        if len(out) + lit_len > size:
            raise ValueError("LZ4 block expands beyond the expected size")
        out += block[pos : pos + lit_len]
        pos += lit_len
        if pos == total:
            break
        if pos + 2 > total:
            raise ValueError("truncated LZ4 block: offset")
        offset = block[pos] | (block[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(out):
            raise ValueError("invalid LZ4 match offset")
        match_len = token & 15
        if match_len == 15:
            while True:
                if pos >= total:
                    raise ValueError("truncated LZ4 block: match length")
                extra = block[pos]
                pos += 1
                match_len += extra
                if extra != 255:
                    break
        match_len += _MIN_MATCH
        if len(out) + match_len > size:
            raise ValueError("LZ4 block expands beyond the expected size")
        start = len(out) - offset
        for i in range(match_len):  # byte by byte: matches may overlap
            out.append(out[start + i])
    if len(out) != size:
        raise ValueError("LZ4 block is shorter than the expected size")
    return bytes(out)
