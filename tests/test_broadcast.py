"""Broadcast Director unit tests — playlist merge, rundown selection, locks.

Playout playlist logic is pure text — no ffmpeg needed. Program selection
runs against an isolated DB.
"""
import sqlite3
from pathlib import Path

import pytest

from newsroom.broadcast import program as prog
from newsroom.broadcast.playout import HlsPlayout
from newsroom.config import Settings
from newsroom.models.event import CanonicalEvent
from newsroom.storage import repositories as repo


def _playout(tmp_path) -> HlsPlayout:
    settings = Settings(
        {"playout": {
            "width": 1920, "height": 1080, "fps": 30,
            "encoder": "h264_videotoolbox", "audio": "aac",
            "hls": {"dir": str(tmp_path), "segment_time": 4,
                    "list_size": 4}}},
        tmp_path)
    return HlsPlayout(settings)


def _write_prog(pl: HlsPlayout, name: str, seg_nums: list[int],
                tmp_path: Path) -> Path:
    prog_pl = tmp_path / name
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4"]
    for n in seg_nums:
        seg = tmp_path / f"seg_{n:05d}.ts"
        seg.write_bytes(b"\x47" * 188)          # fake but named correctly
        lines += ["#EXTINF:4.000000,", seg.name]
    lines.append("#EXT-X-ENDLIST")
    prog_pl.write_text("\n".join(lines) + "\n")
    return prog_pl


class TestPlayoutMerge:
    def test_master_created_without_endlist(self, tmp_path):
        pl = _playout(tmp_path)
        assert pl.playlist.exists()
        assert "#EXT-X-ENDLIST" not in pl.playlist.read_text()

    def test_merge_appends_segments_no_endlist(self, tmp_path):
        pl = _playout(tmp_path)
        pl._prog_pl = _write_prog(pl, "prog_00000.m3u8", [0, 1, 2], tmp_path)
        pl._merge_new()
        text = pl.playlist.read_text()
        assert "seg_00000.ts" in text and "seg_00002.ts" in text
        assert "#EXT-X-ENDLIST" not in text
        assert pl._prog_seen == 3

    def test_merge_is_incremental(self, tmp_path):
        """A second poll only appends segments written since the first."""
        pl = _playout(tmp_path)
        pl._prog_pl = _write_prog(pl, "prog_00000.m3u8", [0, 1], tmp_path)
        pl._merge_new()
        # ffmpeg keeps rewriting the prog playlist as segments land.
        _write_prog(pl, "prog_00000.m3u8", [0, 1, 2], tmp_path)
        pl._merge_new()
        assert pl.playlist.read_text().count("seg_00001.ts") == 1

    def test_trim_keeps_window_and_prunes(self, tmp_path):
        pl = _playout(tmp_path)
        pl._prog_pl = _write_prog(pl, "prog_00000.m3u8",
                                list(range(20)), tmp_path)
        pl._merge_new()
        pl._trim_master()
        text = pl.playlist.read_text()
        segs = [l for l in text.splitlines() if l.startswith("seg_")]
        assert len(segs) == pl.list_size * 3
        # oldest files pruned, newest retained
        assert not (tmp_path / "seg_00000.ts").exists()
        assert (tmp_path / "seg_00019.ts").exists()
        assert "#EXT-X-MEDIA-SEQUENCE:8" in text

    def test_resume_continues_numbering(self, tmp_path):
        for n in (5, 6, 7):
            (tmp_path / f"seg_{n:05d}.ts").write_bytes(b"x")
        pl = _playout(tmp_path)
        assert pl.next_segment == 8


def _seed_event(conn: sqlite3.Connection, event_id: str, event_type: str,
                impact: str, score: float, conf: float,
                with_video: bool = True) -> None:
    ev = CanonicalEvent(
        event_id=event_id, event_type=event_type, status="active",
        severity=5.0, confidence=conf, first_seen=repo.utcnow(),
        last_updated=repo.utcnow(), title=event_id, impact_score=score,
        impact_level=impact, confidence_score=conf, current_revision=1)
    repo.upsert_event(conn, ev)
    if with_video:
        repo.insert_asset(conn, repo.new_id("asset"), "video",
                          f"/tmp/{event_id}.mp4", "hash", event_id, 1, {})


class TestProgramSelection:
    def test_flash_interrupt_picked(self, conn, settings):
        _seed_event(conn, "eq:flash", "earthquake", "FLASH", 0.95, 0.96)
        p = prog.pick_interrupt(conn, settings)
        assert p and p.kind == "interrupt" and p.event_id == "eq:flash"

    def test_interrupt_not_repeated_after_air(self, conn, settings):
        _seed_event(conn, "eq:flash", "earthquake", "FLASH", 0.95, 0.96)
        repo.log_air(conn, "a1", "interrupt", "eq:flash", 1, None,
                     "/tmp/x.mp4", 40.0)
        assert prog.pick_interrupt(conn, settings) is None

    def test_low_confidence_no_interrupt(self, conn, settings):
        _seed_event(conn, "eq:weak", "earthquake", "FLASH", 0.95, 0.80)
        assert prog.pick_interrupt(conn, settings) is None

    def test_no_video_asset_no_air(self, conn, settings):
        _seed_event(conn, "eq:norender", "earthquake", "FLASH", 0.95, 0.96,
                    with_video=False)
        assert prog.pick_interrupt(conn, settings) is None
        assert prog.pick_next(conn, settings) is None

    def test_rundown_picks_top_story(self, conn, settings):
        _seed_event(conn, "eq:a", "earthquake", "UPDATE", 0.6, 0.95)
        _seed_event(conn, "eq:b", "earthquake", "NORMAL", 0.3, 0.95)
        p = prog.pick_next(conn, settings)
        assert p and p.event_id == "eq:a"  # highest impact_score wins

    def test_segment_cadence_blocks_reair(self, conn, settings):
        _seed_event(conn, "eq:a", "earthquake", "UPDATE", 0.6, 0.95)
        first = prog.pick_next(conn, settings)
        assert first
        repo.log_air(conn, "a1", "story", "eq:a", 1, first.rundown_id,
                     "/tmp/x.mp4", 40.0)
        assert prog.pick_next(conn, settings) is None


class TestPlistRendering:
    def test_templates_render_valid_plist(self):
        import plistlib
        from newsroom.macos.services import LAUNCHD_DIR, render_plist
        for tpl in LAUNCHD_DIR.glob("*.plist"):
            parsed = plistlib.loads(render_plist(tpl).encode())
            assert parsed["Label"] == tpl.stem
            assert "@" not in str(parsed["ProgramArguments"])
            assert parsed["RunAtLoad"] is True
