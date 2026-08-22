"""TjipTemp — desktop software for the ESP32-S3 thermometer board.

Layering, bottom up:

``protocol``     framing, message codecs, channel identities. No I/O, no Qt.
``sensors``      PT1000 / type-K / NTC physics. Pure functions over numpy.
``calibration``  models stored in the board's NVS, and the fitting that produces them.
``transport``    USB CDC, TCP, BLE — byte pipes carrying frames.
``core``         timebase fitting, sample ordering and deduplication, ring buffers.
``device``       a board: links, handshake, streaming, backfill, control.
``storage``      the recording database and every export format.
``api``          REST + WebSocket for other programs.
``ui``           PySide6. Depends on everything; nothing depends on it.
``simulator``    a virtual board, so all of the above is testable without hardware.

Only the UI layer imports Qt. The API server and the test suite drive the same
device layer headlessly, which is what keeps the protocol honest.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "APP_NAME", "APP_ID", "ORG_NAME"]

APP_NAME = "TjipTemp"
APP_ID = "io.github.tjiptemp"
ORG_NAME = "TjipTemp"
