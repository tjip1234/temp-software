"""Framing, message codecs and the sample block layout."""

from __future__ import annotations

import numpy as np
import pytest

from tjiptemp.protocol import messages as M
from tjiptemp.protocol.framing import (
    Frame,
    FrameReader,
    FramingError,
    _crc16_reference,
    cobs_decode,
    cobs_encode,
    crc16,
    decode_frame,
    encode_frame,
)


def test_crc16_ccitt_false_reference_vector():
    # The canonical check value for CRC-16/CCITT-FALSE over "123456789".
    assert crc16(b"123456789") == 0x29B1


def test_crc16_matches_the_longhand_definition():
    """The fast path delegates to binascii; this is what says it is the same CRC.

    ``crc16`` is ``binascii.crc_hqx``, which is C and some forty times quicker
    than the byte loop the firmware mirrors. That is only safe as long as the two
    agree on every input, including the seeds and lengths the wire actually uses.
    """
    import os
    import random

    random.seed(20260908)
    cases = [b"", b"\x00", b"\xff", bytes(range(256)), b"123456789"]
    cases += [os.urandom(random.randint(1, 2048)) for _ in range(200)]
    for data in cases:
        assert crc16(data) == _crc16_reference(data), data[:32]
        # The seed is exposed for incremental use; it must track too.
        assert crc16(data, 0x1D0F) == _crc16_reference(data, 0x1D0F)


def test_cobs_encode_run_boundaries():
    """The 254-byte run boundary is where a hand-rolled COBS usually breaks."""
    for length in (0, 1, 253, 254, 255, 256, 507, 508, 509):
        for tail in (b"", b"\x00", b"\x00A", b"A"):
            payload = b"\x01" * length + tail
            encoded = cobs_encode(payload)
            assert 0 not in encoded
            assert cobs_decode(encoded) == payload


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00",
        b"\x00" * 300,
        b"\x01\x02\x03",
        bytes(range(256)),
        b"\xff" * 600,          # forces COBS block splitting at 254 bytes
        bytes(range(256)) * 4,
    ],
)
def test_cobs_roundtrip_and_no_zero_bytes(payload):
    encoded = cobs_encode(payload)
    assert 0 not in encoded, "COBS output must never contain the delimiter"
    assert cobs_decode(encoded) == payload


def test_frame_roundtrip():
    original = Frame(M.Msg.STATUS, b'{"hello":true}', seq=1234, flags=0x01)
    decoded = decode_frame(encode_frame(original)[:-1])  # strip the delimiter
    assert decoded.msg_type == original.msg_type
    assert decoded.payload == original.payload
    assert decoded.seq == original.seq
    assert decoded.is_response


def test_corrupt_frame_is_rejected():
    raw = bytearray(encode_frame(Frame(M.Msg.HELLO, b"abcdef", seq=7)))
    raw[4] ^= 0xFF  # flip a bit in the middle
    with pytest.raises(FramingError):
        decode_frame(bytes(raw[:-1]))


def test_reader_resyncs_after_garbage():
    good = encode_frame(Frame(M.Msg.HELLO, b"one", seq=1))
    junk = b"\x99\x88\x77\x00"  # a plausible burst of line noise
    also_good = encode_frame(Frame(M.Msg.HELLO, b"two", seq=2))

    reader = FrameReader()
    frames = reader.feed(junk + good + junk + also_good)
    assert [f.payload for f in frames] == [b"one", b"two"]
    # The leading junk is what a port hands over when opened mid-frame, on nearly
    # every connect; only the junk between two good frames says the link is bad.
    assert reader.bad_frames == 1
    assert reader.dropped_bytes == 6


def test_reader_handles_byte_at_a_time_delivery():
    """A serial port hands over whatever happened to be in the FIFO."""
    data = b"".join(encode_frame(Frame(M.Msg.HELLO, f"n{i}".encode(), seq=i)) for i in range(1, 6))
    reader = FrameReader()
    out = []
    for byte in data:
        out.extend(reader.feed(bytes([byte])))
    assert [f.seq for f in out] == [1, 2, 3, 4, 5]


def test_sample_block_roundtrip():
    rng = np.random.default_rng(0)
    data = rng.normal(21.0, 0.5, size=(37, 5)).astype(np.float32)
    data[3, 2] = np.nan  # a faulted reading
    block = M.SampleBlock(
        first_seq=1000, t0_us=123456789, dt_us=10000,
        channel_ids=(0, 1, 3, 9, 14), data=data, fault_mask=0b00100,
    )
    decoded = M.decode_sample_block(M.encode_sample_block(block))

    assert decoded.first_seq == 1000
    assert decoded.t0_us == 123456789
    assert decoded.dt_us == 10000
    assert decoded.channel_ids == (0, 1, 3, 9, 14)
    assert decoded.faulted_channels() == (3,)
    np.testing.assert_array_equal(np.isnan(decoded.data), np.isnan(data))
    np.testing.assert_allclose(decoded.data[~np.isnan(decoded.data)],
                               data[~np.isnan(data)], rtol=0, atol=0)


def test_sample_block_sequence_and_time_derivation():
    block = M.SampleBlock(
        first_seq=50, t0_us=1_000_000, dt_us=10_000,
        channel_ids=(0,), data=np.zeros((10, 1), dtype=np.float32),
    )
    assert block.last_seq == 59
    np.testing.assert_array_equal(block.sequences(), np.arange(50, 60))
    assert block.device_times_us()[-1] == 1_000_000 + 9 * 10_000


def test_sample_block_slice():
    block = M.SampleBlock(
        first_seq=100, t0_us=0, dt_us=1000,
        channel_ids=(0, 1), data=np.arange(40, dtype=np.float32).reshape(20, 2),
    )
    piece = block.slice_seq(105, 109)
    assert piece is not None
    assert piece.first_seq == 105
    assert piece.n_samples == 5
    assert piece.t0_us == 5000
    assert block.slice_seq(200, 300) is None


def test_time_sync_codec():
    payload = M.encode_time_echo(111, 222, 333)
    assert M.decode_time_echo(payload) == (111, 222, 333)
    assert M.decode_time_sync(M.encode_time_sync(999)) == 999


def test_json_payload_rejects_nan():
    """NaN belongs in binary sample blocks, never in a JSON control message."""
    with pytest.raises(ValueError):
        M.json_payload({"value": float("nan")})
    assert M.clean_floats({"value": float("nan")}) == {"value": None}


def test_truncated_sample_block_is_an_error():
    block = M.SampleBlock(
        first_seq=0, t0_us=0, dt_us=1000, channel_ids=(0, 1),
        data=np.zeros((10, 2), dtype=np.float32),
    )
    payload = M.encode_sample_block(block)
    with pytest.raises(M.ProtocolError):
        M.decode_sample_block(payload[:-9])
