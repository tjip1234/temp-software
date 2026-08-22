"""COBS framing and CRC-16/CCITT-FALSE for TJIP-1.

Wire format, matching ``docs/protocol.md`` §2::

    <COBS( header || payload || crc16 )> 0x00

The header is 6 bytes and the CRC covers header+payload. COBS guarantees the
delimiter byte never appears inside an encoded frame, so a receiver that has lost
sync only ever has to scan forward to the next 0x00.

This module is deliberately free of any Qt or asyncio dependency: it is pure bytes
in, bytes out, and is the piece the firmware mirrors in ``firmware-ref/tjip_proto.c``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER = struct.Struct("<BBHH")  # msg_type, flags, seq, payload_len
HEADER_SIZE = HEADER.size  # 6
CRC_SIZE = 2
DELIMITER = b"\x00"

FLAG_RESPONSE = 0x01
FLAG_ERROR = 0x02
FLAG_MORE = 0x04

DEFAULT_MAX_PAYLOAD = 1024
ABSOLUTE_MAX_PAYLOAD = 65535


class FramingError(Exception):
    """A frame could not be decoded. Recoverable: the caller resyncs on the next delimiter."""


# --------------------------------------------------------------------------- CRC

def _build_crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC_TABLE = _build_crc_table()


def crc16(data: bytes, seed: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor."""
    crc = seed
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


# -------------------------------------------------------------------------- COBS

def cobs_encode(data: bytes) -> bytes:
    """Consistent Overhead Byte Stuffing. Output contains no 0x00 bytes."""
    out = bytearray()
    code_index = 0
    out.append(0)  # placeholder for the first code byte
    code = 1
    for byte in data:
        if byte != 0:
            out.append(byte)
            code += 1
            if code != 0xFF:
                continue
        out[code_index] = code
        code_index = len(out)
        out.append(0)
        code = 1
    out[code_index] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """Inverse of :func:`cobs_encode`. Raises FramingError on a malformed block."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise FramingError("zero code byte inside COBS block")
        i += 1
        end = i + code - 1
        if end > n:
            raise FramingError("COBS block overruns its buffer")
        out += data[i:end]
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


# ------------------------------------------------------------------------ frames

@dataclass(slots=True)
class Frame:
    msg_type: int
    payload: bytes = b""
    seq: int = 0
    flags: int = 0

    @property
    def is_response(self) -> bool:
        return bool(self.flags & FLAG_RESPONSE)

    @property
    def is_error(self) -> bool:
        return bool(self.flags & FLAG_ERROR)

    @property
    def has_more(self) -> bool:
        return bool(self.flags & FLAG_MORE)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        from .messages import MSG_NAMES

        name = MSG_NAMES.get(self.msg_type, f"0x{self.msg_type:02X}")
        bits = "".join(
            c for c, f in (("R", FLAG_RESPONSE), ("E", FLAG_ERROR), ("M", FLAG_MORE))
            if self.flags & f
        )
        return f"<Frame {name} seq={self.seq} {bits} len={len(self.payload)}>"


def encode_frame(frame: Frame) -> bytes:
    """Serialise a frame including COBS encoding and the trailing delimiter."""
    if len(frame.payload) > ABSOLUTE_MAX_PAYLOAD:
        raise FramingError(f"payload of {len(frame.payload)} bytes exceeds the 16-bit length field")
    head = HEADER.pack(frame.msg_type, frame.flags, frame.seq & 0xFFFF, len(frame.payload))
    body = head + frame.payload
    body += struct.pack("<H", crc16(body))
    return cobs_encode(body) + DELIMITER


def decode_frame(block: bytes) -> Frame:
    """Decode one COBS block (delimiter already stripped)."""
    raw = cobs_decode(block)
    if len(raw) < HEADER_SIZE + CRC_SIZE:
        raise FramingError(f"runt frame: {len(raw)} bytes")
    msg_type, flags, seq, payload_len = HEADER.unpack_from(raw, 0)
    expected = HEADER_SIZE + payload_len + CRC_SIZE
    if len(raw) != expected:
        raise FramingError(f"length mismatch: header says {expected} bytes, got {len(raw)}")
    (got_crc,) = struct.unpack_from("<H", raw, HEADER_SIZE + payload_len)
    want_crc = crc16(raw[: HEADER_SIZE + payload_len])
    if got_crc != want_crc:
        raise FramingError(f"CRC mismatch: got 0x{got_crc:04X}, computed 0x{want_crc:04X}")
    return Frame(
        msg_type=msg_type,
        payload=raw[HEADER_SIZE : HEADER_SIZE + payload_len],
        seq=seq,
        flags=flags,
    )


class FrameReader:
    """Incremental byte-stream to frame decoder.

    Feed it whatever the transport hands you; it yields whole frames and quietly
    survives corruption, truncation and mid-stream connection. Bad blocks are
    counted rather than raised, because on a serial link the first bytes after
    opening the port are routinely garbage and that is not worth an exception.
    """

    def __init__(self, max_block: int = 3 * ABSOLUTE_MAX_PAYLOAD) -> None:
        self._buf = bytearray()
        self._max_block = max_block
        self.bad_frames = 0
        self.dropped_bytes = 0
        self.good_frames = 0

    def feed(self, data: bytes) -> list[Frame]:
        frames: list[Frame] = []
        self._buf += data
        while True:
            idx = self._buf.find(0x00)
            if idx < 0:
                if len(self._buf) > self._max_block:
                    # No delimiter in an implausibly long run: the stream is junk.
                    self.dropped_bytes += len(self._buf)
                    self._buf.clear()
                break
            block = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if not block:
                continue  # back-to-back delimiters, or leading padding
            try:
                frames.append(decode_frame(block))
                self.good_frames += 1
            except FramingError:
                self.bad_frames += 1
                self.dropped_bytes += len(block)
        return frames

    def reset(self) -> None:
        self._buf.clear()
