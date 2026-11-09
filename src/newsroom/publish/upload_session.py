"""Private, crash-safe journal for YouTube resumable upload sessions."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ..paths import output_dir


def session_url_ok(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "www.googleapis.com"
            and parsed.port in (None, 443) and not parsed.username
            and not parsed.password and not parsed.fragment
            and parsed.path == "/upload/youtube/v3/videos")


@contextmanager
def locked_session(asset_id: str, directory: Path | None = None):
    """Serialize uploads per asset; session URL is a bearer capability."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", asset_id):
        raise ValueError("Invalid asset ID for upload journal")
    directory = directory or output_dir() / "youtube_sessions"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise PermissionError("YouTube session directory must be private (0700)")
    lock_path = directory / f"{asset_id}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        if lock_path.stat().st_mode & 0o077:
            raise PermissionError("YouTube session lock must be private")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory / f"{asset_id}.json"


def load_session(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise PermissionError("YouTube upload journal must be a private file")
    return json.loads(path.read_text())


def save_session(path: Path, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
