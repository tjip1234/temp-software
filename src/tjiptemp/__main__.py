"""``tjiptemp`` — the command line entry point.

Three modes:

    tjiptemp                      the GUI
    tjiptemp --headless           API server only, no Qt, for a logging box
    tjiptemp --list               enumerate candidate boards and exit

The headless mode is not a GUI with the window hidden: it runs the same device
layer without importing Qt at all, which is what makes it usable on a Raspberry Pi
sitting next to an experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from . import APP_NAME, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tjiptemp",
        description=f"{APP_NAME} — desktop software for the ESP32-S3 thermometer board",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument(
        "-c", "--connect", action="append", default=[], metavar="ADDRESS",
        help="connect at startup: /dev/ttyACM0, COM7, 192.168.1.44, or a BLE MAC "
             "(repeatable)",
    )
    parser.add_argument("--headless", action="store_true",
                        help="run the API server without a GUI")
    parser.add_argument("--list", action="store_true",
                        help="list candidate boards and exit")
    parser.add_argument("--record", action="store_true",
                        help="headless: start recording every connected board immediately")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="recordings database to use")
    parser.add_argument("--api-host", default=None)
    parser.add_argument("--api-port", type=int, default=None)
    parser.add_argument("--no-api", action="store_true", help="do not start the API server")
    parser.add_argument("--broadcast", action="store_true",
                        help="publish a channel as a network thermometer "
                             "(VNA Studio and anything else speaking that protocol)")
    parser.add_argument("--no-broadcast", action="store_true",
                        help="do not publish the thermometer, whatever the settings say")
    parser.add_argument("--broadcast-port", type=int, default=None, metavar="PORT")
    parser.add_argument("--broadcast-channel", type=int, default=None, metavar="ID",
                        help="channel id to publish, or -1 to pick the best probe")
    parser.add_argument("--broadcast-name", default=None, metavar="NAME",
                        help="name shown in the client's device list")
    parser.add_argument("--rate", type=float, default=None, metavar="HZ",
                        help="override each board's own sample rate (without this, "
                             "the rate stored on the board is used)")
    parser.add_argument("--auto-connect", action="store_true",
                        help="scan USB and connect to every board that answers "
                             "(off by default, whatever the settings say)")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


#: Per log file, and how many rotated ones to keep: a few days of connects,
#: disconnects and errors, at a size nobody has to think about.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3


def configure_logging(verbosity: int):
    """Console at the verbosity asked for, plus a rotating log file, always.

    The file is what makes "it took ages to connect" diagnosable after the
    fact, from the timings of the attempt that actually did -- and a windowed
    build on Windows or macOS has no console to read at all. It records INFO
    and up, or everything under -vv. Returns its path, or None if it could not
    be opened.
    """
    from logging.handlers import RotatingFileHandler

    from .core.application import log_path

    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    file_level = min(level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(file_level)
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(console)

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("not keeping a log file at %s: %s", path, exc)
        path = None
    else:
        handler.setLevel(file_level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)

    # These are chatty at DEBUG and rarely what you are looking for.
    for noisy in ("asyncio", "matplotlib", "PIL", "bleak", "zeroconf"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))

    import platform
    logging.getLogger(__name__).info(
        "%s %s starting (Python %s, %s)", APP_NAME, __version__,
        platform.python_version(), platform.platform())
    return path


def apply_overrides(settings, args) -> None:
    if args.db:
        settings.db_path = args.db
    if args.api_host:
        settings.api_host = args.api_host
    if args.api_port:
        settings.api_port = args.api_port
    if args.no_api:
        settings.api_enabled = False
    if args.rate:
        settings.stream_rate_hz = args.rate
    if args.auto_connect:
        settings.auto_connect_usb = True
    if args.broadcast:
        settings.broadcast_enabled = True
    if args.no_broadcast:
        settings.broadcast_enabled = False
    if args.broadcast_port:
        settings.broadcast_port = args.broadcast_port
    if args.broadcast_channel is not None:
        settings.broadcast_channel = args.broadcast_channel
    if args.broadcast_name:
        settings.broadcast_name = args.broadcast_name


async def run_list() -> int:
    from .transport.discovery import discover_all

    candidates = await discover_all(usb=True, wifi=True, bluetooth=False, all_ports=True)
    if not candidates:
        print("No candidate devices found.")
        print("Plug a board in over USB. On Linux you may need to be in the "
              "'dialout' group (or 'uucp' on Arch) to see serial ports.")
        return 1
    print(f"{'KIND':<6} {'ADDRESS':<28} DETAIL")
    for candidate in candidates:
        print(f"{candidate.kind.upper():<6} {candidate.address:<28} {candidate.label}")
    print("\nA port is only confirmed as a board once it answers, so try connecting.")
    return 0


async def run_headless(args) -> int:
    from .core.application import Application, Settings

    settings = Settings.load()
    apply_overrides(settings, args)
    app = Application(settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)

    try:
        targets = args.connect or []
        if not targets and settings.auto_connect_usb:
            devices = await app.auto_connect()
        else:
            devices = []
            for target in targets:
                try:
                    devices.append(await app.connect(target))
                except Exception as exc:
                    print(f"could not connect to {target}: {exc}", file=sys.stderr)

        if not devices:
            print("No boards connected.", file=sys.stderr)
            if not targets and not settings.auto_connect_usb:
                print("Nothing was tried: pass --connect ADDRESS, or --auto-connect "
                      "to scan USB. Use --list to see what is there.", file=sys.stderr)
        for device in devices:
            print(f"connected: {device.label} over {', '.join(device.link_kinds)}")

        # Connecting adopts each board's stored rate. --rate is the one thing
        # that overrides it, because it was typed on purpose.
        if args.rate:
            for device in devices:
                with contextlib.suppress(Exception):
                    await device.set_config({"acquire": {"rate_hz": args.rate}})
                    await device.start_streaming(args.rate)

        if args.record:
            for device in devices:
                try:
                    recording = app.recorder.start(device)
                    print(f"recording {device.label} into session {recording.session_id}")
                except RuntimeError as exc:
                    print(f"could not record {device.label}: {exc}", file=sys.stderr)

        url = await app.start_api()
        if url:
            print(f"API on {url}  ·  docs at {url}/docs")
        try:
            thermo = await app.start_broadcast()
        except Exception as exc:
            print(f"thermometer broadcast did not start: {exc}", file=sys.stderr)
        else:
            if thermo:
                print(f"Thermometer on {thermo}  ·  discoverable by mDNS and UDP beacon")
        print("Ctrl-C to stop.")
        await stop.wait()
    finally:
        print("\nshutting down…")
        await app.close()
    return 0


def run_gui(args) -> int:
    # Imported here so --headless never touches Qt.
    import qasync
    from PySide6.QtWidgets import QApplication

    from . import APP_ID
    from .core.application import Application, Settings
    from .ui import theme as theme_module
    from .ui.mainwindow import MainWindow

    settings = Settings.load()
    apply_overrides(settings, args)

    qt_app = QApplication(sys.argv[:1])
    # We drive the exit ourselves. Qt's own quit sequence does not wait for
    # anything asynchronous, and the shutdown here is all asynchronous: closing
    # links, flushing buffered rows to the database, stopping the API server.
    # Letting the last window closing quit Qt tears the event loop down first
    # and none of that runs. See the comment on ``session`` below.
    qt_app.setQuitOnLastWindowClosed(False)
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationDisplayName(APP_NAME)
    qt_app.setOrganizationName(APP_NAME)
    qt_app.setDesktopFileName(APP_ID)
    qt_app.setApplicationVersion(__version__)

    loop = qasync.QEventLoop(qt_app)
    asyncio.set_event_loop(loop)

    app = Application(settings)
    theme = theme_module.resolve(settings.theme)
    theme_module.apply(qt_app, theme)

    window = MainWindow(app)
    window.apply_theme(theme)
    window.show()

    async def startup() -> None:
        try:
            url = await app.start_api()
            if url:
                log = logging.getLogger(__name__)
                log.info("API listening on %s", url)
        except Exception as exc:
            window.statusBar().showMessage(f"API server did not start: {exc}", 8000)

        try:
            await app.start_broadcast()
        except Exception as exc:
            window.statusBar().showMessage(
                f"Thermometer broadcast did not start: {exc}", 8000)

        for target in args.connect:
            with contextlib.suppress(Exception):
                await app.connect(target)
        if not args.connect and settings.auto_connect_usb:
            with contextlib.suppress(Exception):
                await app.auto_connect()

    async def session() -> None:
        """Run until the window closes, then shut down while the loop is alive.

        The previous arrangement -- wait on ``aboutToQuit``, then call
        ``app.close()`` -- could not work: ``aboutToQuit`` fires *during* Qt's
        teardown, so by the time the waiter woke, ``QApplication.exec`` had
        already returned and qasync's loop had stopped. ``run_until_complete``
        raised "Event loop stopped before Future completed" and ``app.close()``
        never ran at all, which lost buffered recording rows, left the serial
        port open with the board still streaming, and silently discarded any
        settings changed during the session.

        Here the close is just another step in a coroutine on a running loop.
        """
        boot = asyncio.ensure_future(startup())
        try:
            await window.closed.wait()
        finally:
            boot.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await boot
            await app.close()

    # Ctrl-C would otherwise kill the process between a flush and its commit.
    # It sets the event directly rather than calling window.close(): a close
    # event can raise the "recordings in progress" prompt, and opening a modal
    # from an event-loop callback is the nested-loop deadlock that
    # ui.widgets.message_later exists to avoid. Shutting down is the answer to
    # Ctrl-C anyway, and app.close() stops the recordings cleanly on the way.
    with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError):
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, window.closed.set)

    with loop:
        loop.run_until_complete(session())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    if args.list:
        return asyncio.run(run_list())
    if args.headless:
        try:
            return asyncio.run(run_headless(args))
        except KeyboardInterrupt:
            return 130
    try:
        return run_gui(args)
    except ImportError as exc:
        print(
            f"The graphical interface needs PySide6 and pyqtgraph ({exc}).\n"
            f"Install them, or run headless:  tjiptemp --headless",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
