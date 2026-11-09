"""Runtime boundaries for untrusted feeds and loggable errors."""
from __future__ import annotations

import os
import re
import stat
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

_MAX_RESPONSE_BYTES = 24 * 1024 * 1024
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api_key|key|token|email|password|registrationkey)=)[^&#\s]+")
_SECRET_ENV = (
    "FRED_API_KEY", "EIA_API_KEY", "BLS_API_KEY", "ACLED_API_KEY",
    "ACLED_EMAIL", "NEWSROOM_RTMP_URL", "NEWSROOM_YT_CLIENT_SECRET",
    "NEWSROOM_YT_REFRESH_TOKEN",
)


def redact_error(exc: Exception) -> str:
    """Keep diagnostics useful without putting API credentials in logs."""
    text = str(exc)
    for name in _SECRET_ENV:
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _QUERY_SECRET.sub(r"\1[REDACTED]", text)[:300]


def require_private_file(path: Path) -> None:
    """Refuse to load a dotenv file readable by another local account."""
    metadata = path.lstat()
    if (stat.S_ISLNK(metadata.st_mode) or
            not stat.S_ISREG(metadata.st_mode) or
            stat.S_IMODE(metadata.st_mode) & 0o077):
        raise PermissionError(".env must be a regular file with mode 600")


def _validate_url(url: str, allowed_hosts: set[str]) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443)):
        raise ValueError("feed URL must use HTTPS on an approved host")


class _ApprovedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_limited(stream, limit: int) -> bytes:
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError("feed response exceeds size limit")
    return body


def fetch_bytes(
    request: urllib.request.Request, timeout: int,
    *, allowed_hosts: set[str] | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> bytes:
    """Verified HTTPS with a same-host redirect policy and a byte ceiling."""
    origin = urlsplit(request.full_url).hostname
    hosts = allowed_hosts or {origin}
    _validate_url(request.full_url, hosts)
    opener = urllib.request.build_opener(_ApprovedRedirect(hosts))
    with opener.open(request, timeout=timeout) as resp:
        _validate_url(resp.geturl(), hosts)
        return read_limited(resp, max_bytes)
