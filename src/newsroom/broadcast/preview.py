"""Local preview server — watch the broadcast before any YouTube output.

Serves the HLS directory (live.m3u8 + segments) plus a minimal index.html
whose <video> tag plays the stream natively in Safari/QuickTime (HLS is
the one live format macOS plays without JavaScript). Also exposes
/state.json for the director's current state and /status for newsroom
health — the com.newsroom.health contract in one endpoint.
"""
from __future__ import annotations

import json
import sqlite3
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import Settings
from ..intelligence.freshness import feed_status
from ..storage import repositories as repo
from ..storage.database import integrity_check

_INDEX = """<!doctype html><meta charset=utf-8><title>newsroom preview</title>
<style>
body{background:#0b1220;color:#e8eef7;font:16px -apple-system,sans-serif;
 margin:0;display:flex;flex-direction:column;align-items:center}
video{width:100%%;max-width:1280px;background:#000;margin-top:24px}
#s{margin:16px;color:#93a3be;font:13px ui-monospace,monospace;white-space:pre}
b{color:#38bdf8}
</style>
<video src="live.m3u8" controls autoplay muted playsinline></video>
<div id=s>connecting…</div>
<script>
async function tick(){try{const r=await fetch('state.json');const j=await r.json();
document.getElementById('s').innerHTML=
'<b>'+j.state+'</b> — '+(j.on_air||'')+'\\nsince '+(j.since||'');
}catch(e){document.getElementById('s').textContent='off air'}}
setInterval(tick,2000);tick();
</script>
"""


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, conn=None, **kw):
        self._conn = conn
        super().__init__(*a, **kw)

    def do_GET(self):  # noqa: N802 — stdlib hook name
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html", _INDEX.encode())
            return
        if self.path in ("/status", "/status.json"):
            self._send(200, "application/json",
                       json.dumps(self._status()).encode())
            return
        super().do_GET()

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
                out["feeds"] = [feed_status(conn, s)
                                for s in ("usgs", "nws", "eonet", "gdacs")]
                out["jobs"] = repo.queue_depth(conn)
                out["air_log"] = [dict(r)
                                  for r in repo.air_log_tail(conn, 10)]
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
    handler = partial(_Handler, directory=str(directory), conn=conn_factory)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.serve_forever()
