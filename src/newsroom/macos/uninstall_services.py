"""Uninstall newsroom launchd services (§38). See services.py."""
from __future__ import annotations

import sys

from .services import uninstall

if __name__ == "__main__":
    sys.exit(uninstall())
