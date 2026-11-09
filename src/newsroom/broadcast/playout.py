"""HLS playout — the Director owns live.m3u8.

Each aired program is its own ffmpeg encode writing a private program
playlist (prog_NNNN.m3u8) plus segments in the shared seg_*.ts namespace.
While the encode runs, the playout merges new EXTINF lines from the program
playlist into the master live.m3u8 — so the master is a continuously
updating live playlist with no EXT-X-ENDLIST ever. A viewer polling during
a program boundary keeps a valid playlist and simply picks up the next
segments; EXT-X-DISCONTINUITY marks each program join.

`-re` paces input at natural speed: a 40 s story takes 40 s to air.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from ..config import Settings
from ..editor import ffmpeg
from ..logging import get_logger, log_event
from ..paths import resolve

log = get_logger("playout")

_SEG_RE = re.compile(r"^seg_\d+\.ts$")


class HlsPlayout:
    def __init__(self, settings: Settings):
        self.settings = settings
        hls = settings.get("playout.hls", {}) or {}
        self.dir = resolve(hls.get("dir", "output/preview"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.playlist = self.dir / hls.get("playlist", "live.m3u8")
        self.seg_time = int(hls.get("segment_time", 4))
        self.list_size = int(hls.get("list_size", 12))
        self.next_segment = self._resume_segment_number()
        self._media_seq = self._resume_media_sequence()
        self._proc: subprocess.Popen | None = None
        self._prog_pl: Path | None = None
        self._prog_seen = 0          # EXTINF lines already merged
        self._ensure_master()

    # -- resume state ------------------------------------------------------
    def _resume_segment_number(self) -> int:
        highest = -1
        for seg in self.dir.glob("seg_*.ts"):
            m = re.match(r"seg_(\d+)\.ts", seg.name)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest + 1

    def _resume_media_sequence(self) -> int:
        """Sequence number of the first segment in the existing master."""
        if self.playlist.exists():
            for line in self.playlist.read_text().splitlines():
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    return int(line.split(":")[1])
        return 0

    def _ensure_master(self) -> None:
        if self.playlist.exists():
            return
        self._write_master(header_only=True)

    def _write_master(self, lines: list[str] | None = None,
                      header_only: bool = False) -> None:
        out = ["#EXTM3U", "#EXT-X-VERSION:3",
               f"#EXT-X-TARGETDURATION:{self.seg_time + 1}",
               f"#EXT-X-MEDIA-SEQUENCE:{self._media_seq}"]
        if not header_only and lines:
            out += lines
        tmp = self.playlist.with_suffix(".tmp")
        tmp.write_text("\n".join(out) + "\n")
        os.replace(tmp, self.playlist)

    def _master_lines(self) -> list[str]:
        if not self.playlist.exists():
            return []
        return [l for l in self.playlist.read_text().splitlines() if l]

    def _append_master(self, lines: list[str]) -> None:
        """Append EXTINF/seg/discontinuity lines to the live playlist."""
        with self.playlist.open("a") as fh:
            fh.write("\n".join(lines) + "\n")

    # -- program encodes ---------------------------------------------------
    def _hls_out_args(self, prog_pl: Path) -> list[str]:
        return [
            "-f", "hls",
            "-hls_time", str(self.seg_time),
            "-hls_list_size", "0",
            "-hls_segment_filename", str(self.dir / "seg_%05d.ts"),
            "-start_number", str(self.next_segment),
            str(prog_pl),
        ]

    def _encode_args(self) -> list[str]:
        p = self.settings.get("playout", {}) or {}
        enc = p.get("encoder", "libx264")
        args = ["-c:v", enc]
        if "x264" in enc:
            # True CBR + fixed GOP — what live ingest expects. VideoToolbox's
            # rate control has no floor and undershoots to ~1.5 Mbps on the
            # mostly-static newsroom picture. minrate alone does NOT fix
            # this (x264 VBV only caps peaks) — nal-hrd=cbr inserts filler
            # NALs to hold the mux rate at the target, which is the actual
            # CBR contract YouTube's "not enough video" warning wants.
            fps = int(p.get("fps", 30))
            gop = fps * int(p.get("gop_seconds", 2))
            br = p.get("video_bitrate", "6800k")
            args += ["-preset", p.get("preset", "veryfast"),
                     "-b:v", br, "-minrate", br, "-maxrate", br,
                     "-bufsize", p.get("bufsize", "13600k"),
                     "-g", str(gop), "-keyint_min", str(gop),
                     "-sc_threshold", "0",
                     "-x264-params", "nal-hrd=cbr"]
        else:
            args += ["-b:v", p.get("video_bitrate", "6000k")]
        return args + ["-c:a", p.get("audio", "aac"), "-ar", "48000",
                       "-b:a", p.get("audio_bitrate", "160k"), "-ac", "2"]

    def _geom(self) -> tuple[int, int, int]:
        p = self.settings.get("playout", {}) or {}
        return (int(p.get("width", 1920)), int(p.get("height", 1080)),
                int(p.get("fps", 30)))

    def _ticker_overlay(self, base: str, ticker_index: int,
                        height: int) -> list[str]:
        """Scroll a transparent ticker tile across the lower third."""
        from .frame import TICKER_H, TICKER_X
        speed = int(self.settings.get("playout.ticker_speed", 110))
        return [
            f"[{ticker_index}:v]format=rgba[tick]",
            f"{base}[tick]overlay=x={TICKER_X}-"
            f"mod(t*{speed}\\,overlay_w):y={height - TICKER_H}:"
            "eval=frame[v]",
        ]

    def air_video(self, src: Path, frame: Path | None = None,
                  ticker: Path | None = None) -> int:
        w, h, fps = self._geom()
        if frame:
            # Channel chrome: still frame + content fitted into the window
            # between header and ticker, centered — any aspect is safe.
            from .frame import WINDOW_H, WINDOW_W
            inputs = ["-re", "-loop", "1", "-i", str(frame),
                      "-re", "-i", str(src)]
            chains = [
                f"[1:v]scale={WINDOW_W}:{WINDOW_H}:"
                "force_original_aspect_ratio=decrease,"
                f"fps={fps},format=yuv420p[vid]",
            ]
            base = "[base]" if ticker else "[v]"
            chains.append(
                f"[0:v][vid]overlay=(W-w)/2:(H-h)/2:shortest=1{base}")
            if ticker:
                inputs += ["-re", "-loop", "1", "-i", str(ticker)]
                chains += self._ticker_overlay("[base]", 2, h)
            return self._start_program(
                inputs, ["-filter_complex", ";".join(chains),
                         "-map", "[v]", "-map", "1:a?"])
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0b1220,"
              f"fps={fps},format=yuv420p")
        if ticker:
            chains = [f"[0:v]{vf}[base]"]
            chains += self._ticker_overlay("[base]", 1, h)
            return self._start_program(
                ["-re", "-i", str(src),
                 "-re", "-loop", "1", "-i", str(ticker)],
                ["-filter_complex", ";".join(chains),
                 "-map", "[v]", "-map", "0:a?"])
        return self._start_program(["-re", "-i", str(src)], ["-vf", vf])

    def air_slate(self, slate_png: Path, seconds: float,
                  ticker: Path | None = None) -> int:
        w, h, fps = self._geom()
        vf = f"scale={w}:{h},fps={fps},format=yuv420p"
        inputs = [
            "-re", "-loop", "1", "-t", str(seconds), "-i", str(slate_png),
            "-re", "-f", "lavfi", "-t", str(seconds),
            "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
        ]
        if ticker:
            inputs += ["-re", "-loop", "1", "-t", str(seconds),
                       "-i", str(ticker)]
            chains = [f"[0:v]{vf}[base]"]
            chains += self._ticker_overlay("[base]", 2, h)
            return self._start_program(
                inputs, ["-filter_complex", ";".join(chains),
                         "-map", "[v]", "-map", "1:a"])
        return self._start_program(inputs, ["-vf", vf])

    def _start_program(self, input_args: list[str],
                       vmap: list[str] | None = None) -> int:
        prog_pl = self.dir / f"prog_{self.next_segment:05d}.m3u8"
        args = [*input_args, *(vmap or []), *self._encode_args(),
                *self._hls_out_args(prog_pl)]
        cmd = [ffmpeg.ffmpeg_path(), "-y", "-hide_banner",
               "-loglevel", "error", *args]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self._prog_pl = prog_pl
        self._prog_seen = 0
        # Program boundary marker goes in at join time, not segment time.
        self._append_master(["#EXT-X-DISCONTINUITY"])
        return self._proc.pid

    # -- playlist merge ----------------------------------------------------
    def _merge_new(self) -> None:
        """Copy new EXTINF+segment pairs from the program playlist into the
        master. Called on every wait() poll so the live edge stays fresh."""
        if not self._prog_pl or not self._prog_pl.exists():
            return
        lines = self._prog_pl.read_text().splitlines()
        merged: list[str] = []
        seen = 0
        for i, line in enumerate(lines):
            if line.startswith("#EXTINF:"):
                seen += 1
                if seen > self._prog_seen and i + 1 < len(lines):
                    seg = lines[i + 1].strip()
                    if _SEG_RE.match(seg):
                        merged += [line.strip(), seg]
        if merged:
            self._append_master(merged)
            self._prog_seen = seen
            self.next_segment = self._resume_segment_number()

    def _trim_master(self) -> None:
        """Keep the playlist a bounded sliding window; rewrite atomically."""
        lines = self._master_lines()
        segs = [i for i, l in enumerate(lines) if _SEG_RE.match(l)]
        keep = self.list_size * 3
        if len(segs) <= keep:
            return
        drop_to = segs[-keep]
        # Walk back to the nearest EXTINF/DISCONTINUITY boundary.
        while drop_to > 0 and not lines[drop_to].startswith("#EXT"):
            drop_to -= 1
        dropped = sum(1 for l in lines[:drop_to] if _SEG_RE.match(l))
        self._media_seq += dropped
        self._write_master(lines=lines[drop_to:])
        # Retire segment files no longer referenced.
        referenced = {l for l in lines[drop_to:] if _SEG_RE.match(l)}
        for seg in self.dir.glob("seg_*.ts"):
            if seg.name not in referenced:
                seg.unlink(missing_ok=True)
        for pl in self.dir.glob("prog_*.m3u8"):
            if pl != self._prog_pl:
                pl.unlink(missing_ok=True)

    # -- control -----------------------------------------------------------
    def wait(self, stop_check=None) -> tuple[int, str]:
        proc = self._proc
        if not proc:
            return 0, ""
        while proc.poll() is None:
            self._merge_new()
            if stop_check and stop_check():
                self.interrupt()
                break
            time.sleep(0.25)
        _, err = proc.communicate()
        self._merge_new()          # drain anything written at mux close
        self._trim_master()
        return proc.returncode or 0, (err or "")[-400:]

    def interrupt(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    @property
    def airing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
