"""Structural checks on the UI that a running app would only find by crashing.

These are cheap and they guard a failure mode that is easy to reintroduce and
hard to attribute: a modal dialog opened from inside a coroutine deadlocks the
Qt and asyncio loops against each other, and the traceback names uvicorn rather
than the line that did it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

UI_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "tjiptemp" / "ui"


def _modals_in_coroutines(path: pathlib.Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "QMessageBox"
            ):
                found.append((sub.lineno, node.name, sub.func.attr))
    return found


@pytest.mark.parametrize("path", sorted(UI_DIR.glob("*.py")), ids=lambda p: p.name)
def test_no_modal_dialog_is_opened_from_a_coroutine(path):
    """QMessageBox.exec spins a nested Qt loop.

    qasync pumps asyncio from inside that loop, which then tries to resume some
    other task while the calling one is still marked running:

        RuntimeError: Cannot enter into task <...> while another task
        <...> is being executed

    Use ``widgets.message_later`` instead — it defers the dialog to a later turn
    of the Qt loop, by which time there is no task to re-enter.
    """
    offenders = _modals_in_coroutines(path)
    assert not offenders, "\n".join(
        f"{path.name}:{line} in async {func}() calls QMessageBox.{kind} — "
        f"use message_later(\"{kind}\", ...)"
        for line, func, kind in offenders
    )


def test_message_later_defers_rather_than_showing():
    """The helper must not call into QMessageBox during the caller's turn."""
    from tjiptemp.ui import widgets

    source = ast.parse(pathlib.Path(widgets.__file__).read_text(encoding="utf-8"))
    func = next(
        node for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == "message_later"
    )
    calls = [
        n.func.attr for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "singleShot" in calls, "message_later must defer through a zero-delay timer"


def test_connect_retries_a_link_that_dies_mid_handshake():
    """A board can accept the socket and then reboot before answering HELLO."""
    from tjiptemp.device import device as D

    # Long enough to ride out a boot, and no longer. It used to be 40 s only
    # because every retry reopened the port and reset the board, so the window
    # had to outlast a loop it was itself feeding.
    assert 8.0 <= D.HANDSHAKE_WINDOW_S <= 20.0
    assert 0 < D.HANDSHAKE_RETRY_S <= 5.0
    # A wrong protocol version is an answer, not a dead link, so it must not be
    # retried for the whole window.
    source = pathlib.Path(D.__file__).read_text(encoding="utf-8")
    assert "fatal = isinstance(exc, (M.DeviceError, M.ProtocolError))" in source
    # And the retry must not reopen the port; see the behavioural test in
    # tests/test_device.py for why.
    assert "if not transport.is_open:" in source


def test_wifi_connect_window_outlasts_a_boot():
    """A rebooting board is not listening at all for tens of seconds."""
    from tjiptemp.transport import tcp

    assert tcp.CONNECT_WINDOW_S >= 30.0
    assert tcp.CONNECT_TIMEOUT_S <= tcp.CONNECT_WINDOW_S
    assert 0 < tcp.CONNECT_RETRY_S <= 5.0


def test_serial_port_is_opened_without_pulsing_the_modem_lines():
    """Opening the port must not reset the board.

    An ESP32-S3's USB-Serial-JTAG resets the chip when the host drives DTR/RTS
    the way esptool does to enter the bootloader, and simply opening the CDC
    port is enough. The board comes back with rst:0x15 (USB_UART_CHIP_RESET)
    and no backtrace, because nothing crashed.

    ``serial.Serial(port=...)`` opens inside the constructor, so any line state
    assigned afterwards is applied too late — the pulse has already happened.
    The object must be built closed and opened explicitly.
    """
    import ast
    import pathlib

    src = pathlib.Path(
        __file__
    ).resolve().parents[1] / "src" / "tjiptemp" / "transport" / "serial_cdc.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_blocking_open"
    )

    for call in (n for n in ast.walk(func) if isinstance(n, ast.Call)):
        is_ctor = (
            (isinstance(call.func, ast.Attribute) and call.func.attr == "Serial")
            or (isinstance(call.func, ast.Name) and call.func.id == "Serial")
        )
        if is_ctor:
            assert not call.args and not call.keywords, (
                "serial.Serial() must be constructed with no arguments: passing "
                "port= opens the port in the constructor, which pulses DTR/RTS "
                "and resets the board before the line states can be set."
            )

    opens = [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "open"
    ]
    assert opens, "_blocking_open must open the port explicitly, after setting dtr/rts"


def test_modem_lines_are_written_atomically_and_survive_close():
    """DTR and RTS must move together, and must not be dropped on close.

    The USB-Serial-JTAG unit watches for a *sequence* of DTR/RTS edges, so what
    matters is not where the lines end up but how many separate transfers the
    host takes to get there. pyserial's ``ser.dtr = False`` and ``ser.rts =
    False`` are two ioctls, which is two edges, which is the sequence.

    HUPCL is the other half: with it set the kernel drops both lines again when
    the port closes, so every close is another pair of edges. Connecting is not
    one open -- a scan touches several ports, and a link that drops is opened
    again -- so a reset on close is a reset on every one of those.
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src" / "tjiptemp" / "transport" / "serial_cdc.py").read_text(encoding="utf-8")

    assert "TIOCMSET" in src, (
        "the modem lines must be written in one ioctl, not one per line"
    )
    assert "HUPCL" in src, (
        "HUPCL must be cleared, or closing the port pulses DTR/RTS and resets "
        "the board"
    )
    # And the open path has to actually use it.
    assert "_quiet_modem_lines(ser)" in src
