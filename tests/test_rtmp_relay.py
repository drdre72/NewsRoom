"""RTMP relay — gating and stream args. No network."""
import os

from newsroom.broadcast.rtmp_relay import _ffmpeg_args, rtmp_url


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
