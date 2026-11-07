"""Install newsroom launchd services (§38). See services.py."""
from __future__ import annotations

import sys

from .services import install

if __name__ == "__main__":
    sys.exit(install(print_only="--print" in sys.argv))
