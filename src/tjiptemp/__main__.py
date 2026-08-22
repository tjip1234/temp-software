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
    parser.add_argument("--rate", type=float, default=None, metavar="HZ",
                        help="stream rate to request from each board")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )
    # These are chatty at DEBUG and rarely what you are looking for.
    for noisy in ("asyncio", "matplotlib", "PIL", "bleak", "zeroconf"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))


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
        for device in devices:
            print(f"connected: {device.label} over {', '.join(device.link_kinds)}")

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

        for target in args.connect:
            with contextlib.suppress(Exception):
                await app.connect(target)
        if not args.connect and settings.auto_connect_usb:
            with contextlib.suppress(Exception):
                await app.auto_connect()

    shutdown = asyncio.Event()
    qt_app.aboutToQuit.connect(shutdown.set)

    with loop:
        loop.create_task(startup())
        loop.run_until_complete(shutdown.wait())
        loop.run_until_complete(app.close())
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
