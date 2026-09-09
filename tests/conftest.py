"""Keep the suite out of the real user's configuration.

``Application.close()`` saves settings, so any test that builds one used to
write ``~/.config/TjipTemp/settings.json``. Settings constructed in code no
longer have a save target at all, and this points the whole suite at a
throwaway directory as a second line of defence.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_config():
    with tempfile.TemporaryDirectory(prefix="tjiptemp-test-config-") as path:
        previous = os.environ.get("TJIPTEMP_CONFIG_DIR")
        os.environ["TJIPTEMP_CONFIG_DIR"] = path
        try:
            yield path
        finally:
            if previous is None:
                os.environ.pop("TJIPTEMP_CONFIG_DIR", None)
            else:
                os.environ["TJIPTEMP_CONFIG_DIR"] = previous
