"""Resolve ODYSSEUS_HOME for standalone skill scripts.

Skill scripts may run outside the Odysseus process (e.g. system Python,
nix env, CI) where ``odysseus_constants`` is not importable.  This module
provides the same ``get_odysseus_home()`` and ``display_odysseus_home()``
contracts as ``odysseus_constants`` without requiring it on ``sys.path``.

When ``odysseus_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``odysseus_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``ODYSSEUS_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from odysseus_constants import display_odysseus_home as display_odysseus_home
    from odysseus_constants import get_odysseus_home as get_odysseus_home
except (ModuleNotFoundError, ImportError):

    def get_odysseus_home() -> Path:
        """Return the Odysseus home directory (default: ~/.odysseus).

        Mirrors ``odysseus_constants.get_odysseus_home()``."""
        val = os.environ.get("ODYSSEUS_HOME", "").strip()
        return Path(val) if val else Path.home() / ".odysseus"

    def display_odysseus_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``odysseus_constants.display_odysseus_home()``."""
        home = get_odysseus_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
