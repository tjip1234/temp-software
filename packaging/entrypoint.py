"""Entry script for the bundled builds.

PyInstaller runs its entry script as a top-level module named ``__main__``, not
as part of a package. Pointing it straight at ``src/tjiptemp/__main__.py`` — as
this spec used to — means every ``from . import ...`` in that file raises
"attempted relative import with no known parent package", so the AppImage, the
.app and the .exe all built successfully and then died on launch.

Importing the package and calling into it keeps the relative imports valid, and
keeps the bundled program taking exactly the same path as ``tjiptemp`` installed
from a wheel.
"""

import sys

from tjiptemp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
