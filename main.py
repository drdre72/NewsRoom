#!/usr/bin/env python3
"""newsroom entry point.

Prefer the venv interpreter (`uv run python main.py ...`) or the installed
`newsroom` script. This shim also works when invoked directly, adding src/
to sys.path as a convenience for checkouts without an editable install.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if (SRC / "newsroom" / "cli.py").exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from newsroom.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
