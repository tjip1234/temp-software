"""Shutdown, teardown and reconnect.

Every test here guards a bug that shipped: the application quitting without ever
running its own shutdown, widgets that outlived the thing that owned them, and a
reconnect policy that existed only as three unused attributes.
"""

from __future__ import annotations

import asyncio
import gc
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading

import pytest

from tjiptemp.device.device import Device, DeviceManager
from tjiptemp.simulator.board import SimulatedBoard
from tjiptemp.simulator.server import LoopbackTransport, SimulatorRuntime
from tjiptemp.storage.db import Database

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# --------------------------------------------------------------- application

SHUTDOWN_PROBE = """
import sys
sys.path.insert(0, {src!r})
from PySide6.QtCore import QTimer
import tjiptemp.__main__ as entry
from tjiptemp.core.application import Application
from tjiptemp.ui.mainwindow import MainWindow

calls = []
real_close = Application.close
async def traced(self):
    calls.append("close")
    return await real_close(self)
Application.close = traced

real_show = MainWindow.show
def show(self):
    real_show(self)
    QTimer.singleShot(250, self.close)
MainWindow.show = show

rc = entry.run_gui(entry.build_parser().parse_args(["--no-api", "--no-broadcast"]))
print("RESULT", rc, len(calls))
"""


def test_gui_shutdown_runs_the_async_close(tmp_path):
    """Closing the window must reach Application.close().

    It used to not: the shutdown waited on ``aboutToQuit``, which fires during
    Qt's teardown, so ``run_until_complete`` raised "Event loop stopped before
    Future completed" and the close never ran. Buffered recording rows were
    lost, the serial port stayed open with the board still streaming, and
    settings changed during the session were discarded.

    Run in a subprocess: ``run_gui`` builds its own QApplication, and any other
    test that has made one would otherwise force this to skip -- quietly
    disarming the guard on the worst bug in the file.
    """
    pytest.importorskip("qasync")
    pytest.importorskip("PySide6")

    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    script = tmp_path / "probe.py"
    script.write_text(SHUTDOWN_PROBE.format(src=src), encoding="utf-8")

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["TJIPTEMP_CONFIG_DIR"] = str(tmp_path / "config")
    # Both ends in UTF-8: on Windows the child would otherwise write its
    # output in the ANSI code page, and this side would read it as something else.
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert "RESULT 0 1" in proc.stdout, (
        f"the window closed without shutting the application down\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr[-2000:]}"
    )
    # A clean teardown leaves no orphaned-task noise behind.
    assert "Task was destroyed but it is pending" not in proc.stderr
    assert "Event loop stopped before Future completed" not in proc.stderr


# ------------------------------------------------------------------ widgets

def test_simulator_panel_releases_its_subscription():
    """The panel subscribes to the device; the tab has to hand that back.

    Without it the device holds a bound method of the panel, so the panel is
    never freed and keeps rebuilding itself on every event -- one more leaked
    copy per reconnect, for the life of the process.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from tjiptemp.ui.simpanel import SimulatorPanel
    from tjiptemp.ui.theme import resolve

    qt = QApplication.instance() or QApplication([])
    device = Device(serial="TJIP-TEST00000001", name="probe")
    device.info = {"caps": {"sim": {"pt1000_out": True}}}
    theme = resolve("dark")

    panels = []
    for _ in range(5):
        panel = SimulatorPanel(device, theme)
        panels.append(panel)
        panel.close_panel()
        qt.processEvents()

    assert device._listeners == [], "the panel is still subscribed after close_panel()"

    # The leak was the device retaining a bound method of the panel, which kept
    # the whole widget tree alive and firing. Nothing reachable from the device
    # may lead back to a panel.
    reachable = gc.get_referents(device.__dict__)
    for panel in panels:
        assert panel not in reachable
        assert not any(
            getattr(ref, "__self__", None) is panel for ref in device._listeners
        )
    for panel in panels:
        panel.deleteLater()
    qt.processEvents()


def test_device_tab_closes_the_simulator_panel():
    """close_tab() is the only caller; if it stops calling, the leak is back."""
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "tjiptemp" / "ui" / "devicetab.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    close_tab = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "close_tab"
    )
    assert "close_panel" in ast.dump(close_tab)


def test_calibration_panel_stops_when_its_tab_closes():
    """The panel lives as long as its board's tab, so close_tab() has to stop
    its live timer and any capture still in flight."""
    import ast
    import pathlib

    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from tjiptemp.ui.calibration import CalibrationPanel
    from tjiptemp.ui.theme import resolve

    qt = QApplication.instance() or QApplication([])
    device = Device(serial="TJIP-TEST00000001", name="probe")
    device.info = {"channels": []}

    panel = CalibrationPanel(device, resolve("dark"))
    assert panel._live_timer.isActive()
    panel.close_panel()
    qt.processEvents()
    assert not panel._live_timer.isActive(), "the live timer survived close_panel()"
    panel.deleteLater()

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "tjiptemp" / "ui" / "devicetab.py"
    ).read_text(encoding="utf-8")
    close_tab = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "close_tab"
    )
    assert "attr='calibration'" in ast.dump(close_tab)


# ---------------------------------------------------------------- reconnect

@pytest.fixture
async def runtime():
    board = SimulatedBoard(serial="TJIP-TEST00000001", rate_hz=50.0, seed=3)
    rt = SimulatorRuntime(board)
    await rt.start()
    yield rt
    await rt.stop()


async def test_manager_reconnects_after_an_unexpected_drop(runtime, monkeypatch):
    import tjiptemp.device.device as device_mod

    monkeypatch.setattr(device_mod, "RECONNECT_DELAYS_S", (0.05,))
    monkeypatch.setattr(
        device_mod, "build_transport", lambda candidate: LoopbackTransport(runtime)
    )

    manager = DeviceManager()
    device = await manager.connect(LoopbackTransport(runtime), start_stream=False)
    assert device.links

    # The board went away by itself: close the transport under the link.
    link = next(iter(device.links.values()))
    await link.transport.close()

    for _ in range(200):
        await asyncio.sleep(0.05)
        if device.links:
            break
    assert device.links, "the manager never reconnected after the link died"
    await manager.close()


async def test_manager_does_not_fight_a_deliberate_disconnect(runtime, monkeypatch):
    import tjiptemp.device.device as device_mod

    monkeypatch.setattr(device_mod, "RECONNECT_DELAYS_S", (0.05,))
    monkeypatch.setattr(
        device_mod, "build_transport", lambda candidate: LoopbackTransport(runtime)
    )

    manager = DeviceManager()
    device = await manager.connect(LoopbackTransport(runtime), start_stream=False)
    serial = device.serial

    await manager.disconnect(serial)
    await asyncio.sleep(0.4)

    assert serial not in manager.devices
    assert not device.links, "a disconnect the user asked for was undone"
    assert not manager._reconnect_tasks
    await manager.close()


async def test_manager_close_leaves_no_pending_tasks(runtime, monkeypatch):
    import tjiptemp.device.device as device_mod

    monkeypatch.setattr(device_mod, "RECONNECT_DELAYS_S", (30.0,))
    monkeypatch.setattr(
        device_mod, "build_transport", lambda candidate: LoopbackTransport(runtime)
    )

    manager = DeviceManager()
    device = await manager.connect(LoopbackTransport(runtime), start_stream=False)
    link = next(iter(device.links.values()))
    await link.transport.close()
    await asyncio.sleep(0.1)
    assert manager._reconnect_tasks, "expected a reconnect to be pending"

    await manager.close()
    assert not manager._reconnect_tasks
    leftover = [
        t for t in asyncio.all_tasks()
        if not t.done() and t is not asyncio.current_task()
        and "reconnect" in repr(t).lower()
    ]
    assert not leftover


# ----------------------------------------------------------------- database

def test_close_releases_connections_from_every_thread(tmp_path):
    """Writes run on executor threads; each opens its own sqlite connection.

    close() used to release only the caller's, leaving the rest -- and the WAL
    read marks they hold -- open until the interpreter exited.
    """
    db = Database(tmp_path / "recordings.tjip")
    errors: list[Exception] = []

    def touch() -> None:
        try:
            db.stats()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=touch) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    assert len(db._all) >= 5, "expected one connection per thread plus the owner's"
    opened = list(db._all)
    db.close()
    assert db._all == []
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_auto_connect_is_off_by_default(tmp_path):
    """Starting the program must not claim hardware.

    Opening a serial port takes it away from whatever else has it open, and on
    an ESP32-S3 opening the CDC port moves the modem lines on a board that may
    be in the middle of a measurement. Scanning is a thing to ask for.
    """
    from tjiptemp.core.application import Settings

    assert Settings().auto_connect_usb is False


def test_a_settings_file_from_before_the_change_is_migrated(tmp_path):
    """Changing a default is not enough when every field is written out.

    Every setting is saved, so an existing file carries the old value and wins.
    A default that was wrong for everybody has to be applied to the files that
    already exist too -- once, so that setting it back on afterwards sticks.
    """
    import json

    from tjiptemp.core.application import SETTINGS_VERSION, Settings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"auto_connect_usb": True, "stream_rate_hz": 10.0}),
                    encoding="utf-8")

    migrated = Settings.load(path)
    assert migrated.auto_connect_usb is False
    assert migrated.settings_version == SETTINGS_VERSION

    # Deliberately turning it back on is not undone on the next start.
    migrated.auto_connect_usb = True
    migrated.save()
    assert Settings.load(path).auto_connect_usb is True
