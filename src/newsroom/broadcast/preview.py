"""Local preview server — watch the broadcast before any YouTube output.

Serves the HLS directory (live.m3u8 + segments) plus a minimal index.html
whose <video> tag plays the stream natively in Safari/QuickTime (HLS is
the one live format macOS plays without JavaScript). Also exposes
/state.json for the director's current state and /status for newsroom
health — the com.newsroom.health contract in one endpoint.
"""
from __future__ import annotations

import json
import re
import sqlite3
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Settings
from ..intelligence.source_health import source_statuses
from ..storage import repositories as repo
from ..storage.database import integrity_check
from ..storage.retention import disk_health

_INDEX = """<!doctype html><meta charset=utf-8><title>newsroom preview</title>
<style>
body{background:#0b1220;color:#e8eef7;font:16px -apple-system,sans-serif;
 margin:0;display:flex;flex-direction:column;align-items:center}
video{width:100%%;max-width:1280px;background:#000;margin-top:24px}
#s{margin:16px;color:#93a3be;font:13px ui-monospace,monospace;white-space:pre}
b{color:#38bdf8}
a{color:#38bdf8;text-decoration:none}
a:hover{text-decoration:underline}
</style>
<video src="live.m3u8" controls autoplay muted playsinline></video>
<div id=s>connecting\u2026</div>
<script>
async function tick(){try{const r=await fetch('state.json');const j=await r.json();
const s=document.getElementById('s');const b=document.createElement('b');
b.textContent=j.state||'UNKNOWN';
const txt=' \u2014 '+(j.on_air||'')+'\\nsince '+(j.since||'');
if(j.source_url){const a=document.createElement('a');
a.href=j.source_url;a.target='_blank';a.rel='noopener';
a.textContent='source \u2197';
s.replaceChildren(b,document.createTextNode(txt+'  '),a);}
else{s.replaceChildren(b,document.createTextNode(txt));}
}catch(e){document.getElementById('s').textContent='off air'}}
setInterval(tick,2000);tick();
</script>
"""


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, conn=None, **kw):
        self._conn = conn
        self._settings = kw.pop("settings", None)
        super().__init__(*a, **kw)

    def do_GET(self):  # noqa: N802 — stdlib hook name
        if not self._local_host():
            self.send_error(403)
            return
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html", _INDEX.encode())
            return
        if self.path in ("/status", "/status.json"):
            self._send(200, "application/json",
                       json.dumps(self._status()).encode())
            return
        if not self._public_path():
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):  # noqa: N802 — stdlib hook name
        if not self._local_host():
            self.send_error(403)
        elif self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(_INDEX.encode())))
            self.end_headers()
        elif self.path in ("/status", "/status.json"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
        elif self._public_path():
            super().do_HEAD()
        else:
            self.send_error(404)

    def _local_host(self) -> bool:
        host = self.headers.get("Host", "").split(":")[0].lower()
        return host in ("localhost", "127.0.0.1")

    def _public_path(self) -> bool:
        path = urlsplit(self.path).path
        return path in ("/live.m3u8", "/state.json") or bool(
            re.fullmatch(r"/seg_\d{5,}\.ts", path))

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _status(self) -> dict:
        out = {"service": "newsroom", "ok": True}
        state = Path(self.directory) / "state.json"
        if state.exists():
            out["broadcast"] = json.loads(state.read_text())
        if self._conn is not None:
            # Fresh WAL reader per request — sqlite connections can't cross
            # threads, and ThreadingHTTPServer handles each request on one.
            conn = self._conn()
            try:
                out["db"] = integrity_check(conn)
                out["feeds"] = source_statuses(self._settings)
                out["jobs"] = repo.queue_depth(conn)
                out["disk"] = disk_health(self._settings)
                out["air_log"] = [dict(r)
                                  for r in repo.air_log_tail(conn, 10)]
                out["ok"] = (
                    out["db"]["integrity"] == "ok"
                    and out["disk"]["ok"]
                    and all(f["status"] in ("healthy", "credentials")
                            for f in out["feeds"])
                    and out.get("broadcast", {}).get("state") not in
                    (None, "OFF_AIR")
                )
            finally:
                conn.close()
        return out

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # quiet — requests go to state.json not logs
        pass


def serve(conn: sqlite3.Connection | None, settings: Settings,
          directory: Path, port: int) -> None:
    from ..storage.database import connect
    conn_factory = connect if conn is not None else None
    handler = partial(_Handler, directory=str(directory), conn=conn_factory,
                      settings=settings)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.serve_forever()
