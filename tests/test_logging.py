"""The log file: kept always, so a slow or failed connect can be read back later."""

from __future__ import annotations

import logging

import tjiptemp.__main__ as entry

NOISY = ("asyncio", "matplotlib", "PIL", "bleak", "zeroconf")


def test_connect_lines_reach_the_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TJIPTEMP_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {name: logging.getLogger(name).level for name in NOISY}
    try:
        path = entry.configure_logging(0)
        # INFO is below the console's default of WARNING, and still recorded.
        logging.getLogger("tjiptemp.device.device").info("USB /dev/ttyACM0: online")
        for handler in root.handlers:
            handler.flush()
        assert path == tmp_path / "tjiptemp.log"
        text = path.read_text(encoding="utf-8")
        assert "starting" in text
        assert "USB /dev/ttyACM0: online" in text
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)
        for name, was in levels.items():
            logging.getLogger(name).setLevel(was)
