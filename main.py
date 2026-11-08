#!/usr/bin/env python3
"""newsroom entry point.

Prefer the venv interpreter (`uv run python main.py ...`) or the installed
`newsroom` script. This shim also works when invoked directly, adding src/
to sys.path as a convenience for checkouts without an editable install.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
if (VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve()
        and not os.environ.get("NEWSROOM_NO_REEXEC")):
    os.environ["NEWSROOM_NO_REEXEC"] = "1"
    os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()),
                            *sys.argv[1:]])

SRC = ROOT / "src"
if (SRC / "newsroom" / "cli.py").exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Load .env before any module reads os.environ — launchd agents get only
# PATH/NEWSROOM_ROOT, so API keys (FRED_API_KEY, ACLED_*, NEWSROOM_RTMP_URL)
# must come from the file. Real env vars always win (no override).
ENV_FILE = ROOT / ".env"
if ENV_FILE.exists():
    from newsroom.security import require_private_file
    require_private_file(ENV_FILE)
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

from newsroom.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
