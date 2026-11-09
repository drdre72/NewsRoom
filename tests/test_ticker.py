"""Lower-third ticker — a moving text tile, not a clipped static line."""
from pathlib import Path

from newsroom.broadcast.frame import (
    TICKER_H, TICKER_X, render_frame, render_ticker_strip,
)
from newsroom.broadcast.playout import HlsPlayout
from newsroom.config import Settings


def _playout(tmp_path) -> HlsPlayout:
    settings = Settings(
        {"playout": {
            "width": 1920, "height": 1080, "fps": 30,
            "encoder": "h264_videotoolbox", "audio": "aac",
            "ticker_speed": 110,
            "hls": {"dir": str(tmp_path), "segment_time": 4,
                    "list_size": 4}}},
        tmp_path)
    return HlsPlayout(settings)


def _capture(monkeypatch, playout):
    calls = []

    def start(inputs, vmap=None):
        calls.append((inputs, vmap or []))
        return 123

    monkeypatch.setattr(playout, "_start_program", start)
    return calls


def test_ticker_strip_is_transparent_and_wrap_ready(tmp_path):
    out = tmp_path / "ticker.png"
    render_ticker_strip(
        out, "UPDATE: Flood Warning — Delta  •  NORMAL: Frost Advisory", 1200)
    from PIL import Image
    img = Image.open(out)
    assert img.mode == "RGBA"
    assert img.height == TICKER_H
    assert img.width > 1200
    alpha = img.getchannel("A")
    assert alpha.getpixel((20, 40)) == 0              # viewport starts blank
    assert max(alpha.crop((1200, 0, img.width, img.height)).getdata()) > 0


def test_frame_can_leave_ticker_text_to_encoder(tmp_path):
    frame = render_frame(tmp_path / "frame.png", 1920, 1080,
                         title="Story", ticker=None)
    assert frame.exists()


def test_story_overlay_scrolls_ticker(tmp_path, monkeypatch):
    playout = _playout(tmp_path)
    calls = _capture(monkeypatch, playout)
    ticker = tmp_path / "ticker.png"
    playout.air_video(tmp_path / "story.mp4",
                      frame=tmp_path / "frame.png", ticker=ticker)
    inputs, vmap = calls[0]
    assert str(ticker) in inputs
    fc = vmap[vmap.index("-filter_complex") + 1]
    assert "format=rgba[tick]" in fc
    assert f"overlay=x={TICKER_X}-mod(t*110\\,overlay_w)" in fc
    assert "eval=frame[v]" in fc
    assert "-map" in vmap and "1:a?" in vmap


def test_plain_story_and_slate_can_scroll_ticker(tmp_path, monkeypatch):
    playout = _playout(tmp_path)
    calls = _capture(monkeypatch, playout)
    ticker = tmp_path / "ticker.png"
    playout.air_video(tmp_path / "story.mp4", ticker=ticker)
    playout.air_slate(tmp_path / "slate.png", 20, ticker=ticker)
    assert calls[0][0].count(str(ticker)) == 1
    assert "0:a?" in calls[0][1]
    assert calls[1][0].count(str(ticker)) == 1
    assert "1:a" in calls[1][1]
    for _, vmap in calls:
        fc = vmap[vmap.index("-filter_complex") + 1]
        assert "eval=frame[v]" in fc
