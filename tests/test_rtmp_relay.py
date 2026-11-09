"""RTMP relay — gating and stream args. No network."""
import os

from newsroom.broadcast.rtmp_relay import (
    RtmpRelay, _ffmpeg_args, _newest_segment, rtmp_url,
)


def _on(settings):
    settings.raw()["outputs"]["rtmp"]["enabled"] = True
    settings.raw()["publishing"]["automatic"] = True
    return settings


def test_gated_when_disabled(settings, monkeypatch):
    monkeypatch.setenv("NEWSROOM_RTMP_URL", "rtmp://x/k")
    settings.raw()["outputs"]["rtmp"]["enabled"] = False
    settings.raw()["publishing"]["automatic"] = True
    assert rtmp_url(settings) is None


def test_gated_when_publishing_off(settings, monkeypatch):
    monkeypatch.setenv("NEWSROOM_RTMP_URL", "rtmp://x/k")
    settings.raw()["outputs"]["rtmp"]["enabled"] = True
    settings.raw()["publishing"]["automatic"] = False
    assert rtmp_url(settings) is None


def test_gated_when_env_missing(settings, monkeypatch):
    monkeypatch.delenv("NEWSROOM_RTMP_URL", raising=False)
    _on(settings)
    assert rtmp_url(settings) is None


def test_all_three_gates_pass(settings, monkeypatch):
    monkeypatch.setenv("NEWSROOM_RTMP_URL", "rtmp://a.rtmp.youtube.com/x")
    _on(settings)
    assert rtmp_url(settings) == "rtmp://a.rtmp.youtube.com/x"


def test_ffmpeg_args_copy_and_playlist(settings, monkeypatch):
    from pathlib import Path
    monkeypatch.setenv("NEWSROOM_RTMP_URL", "rtmp://k")
    args = _ffmpeg_args(Path("/tmp/live.m3u8"), "rtmp://k")
    assert args[:1] == [args[0]]
    joined = " ".join(args)
    assert "-c:v copy" in joined and "-f flv" in joined
    assert "aresample=async" in joined   # fills program-boundary audio gaps
    assert str(Path("/tmp/live.m3u8")) in args
    assert "-re" in args and "shell" not in joined


def test_relay_encoder_stays_in_launchd_process_group(settings, monkeypatch):
    import newsroom.broadcast.rtmp_relay as relay

    spawned = []

    class FinishedProcess:
        pid = 123
        returncode = 0

        def poll(self):
            return 0

        def communicate(self):
            return "", ""

    def fake_popen(*args, **kwargs):
        spawned.append(kwargs)
        return FinishedProcess()

    monkeypatch.setattr(relay.subprocess, "Popen", fake_popen)
    instance = RtmpRelay(settings)
    assert instance._stream("rtmp://example/stream") == 0
    assert spawned[0].get("start_new_session") is not True


def test_newest_segment_survives_concurrent_hls_trim(tmp_path, monkeypatch):
    from pathlib import Path

    vanished = tmp_path / "seg_00001.ts"
    remaining = tmp_path / "seg_00002.ts"
    vanished.write_bytes(b"old")
    remaining.write_bytes(b"new")
    real_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == vanished:
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert _newest_segment(tmp_path) == real_stat(remaining).st_mtime
