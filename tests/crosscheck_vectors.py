"""Emit the same test vectors the C reference codec emits, for byte-level diffing.

Run via ``make crosscheck`` in ``firmware-ref/``. Any difference means the host
and the firmware would disagree on the wire, which is the failure mode a written
specification alone cannot catch.
"""

from __future__ import annotations

import numpy as np

from tjiptemp.protocol import messages as M
from tjiptemp.protocol.framing import FLAG_MORE, FLAG_RESPONSE, Frame, encode_frame


def show(label: str, data: bytes) -> None:
    print(f"{label} {data.hex()}")


def main() -> int:
    show("hello", encode_frame(Frame(M.Msg.HELLO, b'{"proto":1}', seq=1)))
    show("empty", encode_frame(Frame(M.Msg.STATUS, b"", seq=0)))

    payload = bytes((i * 7) & 0xFF for i in range(300))
    show("binary", encode_frame(
        Frame(M.Msg.CONFIG, payload, seq=0xBEEF, flags=FLAG_RESPONSE | FLAG_MORE)
    ))

    data = np.array(
        [[21.5 + r, 25.25 + r, 3.9] for r in range(4)], dtype=np.float32
    )
    block = M.SampleBlock(
        first_seq=1000, t0_us=123456789, dt_us=10000,
        channel_ids=(0, 1, 9), data=data, fault_mask=2,
    )
    show("block", M.encode_sample_block(block))

    show("timeecho", M.encode_time_echo(0x1122334455667788, 111, 222))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
