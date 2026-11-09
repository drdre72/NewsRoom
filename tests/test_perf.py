"""Performance-path tests: poll backoff/concurrency, kokoro worker
fallback, and the reusable map browser lifecycle (§22, §37, §75)."""
import time
from pathlib import Path

import pytest


# ------------------------------------------------------------------ polling

class _Adapter:
    name = "fake"

    def __init__(self, name="fake", payloads=("i1",), error=None, delay=0.0,
                 marks=None):
        self.name = name
        self.payloads = payloads
        self.error = error
        self.delay = delay
        self.marks = marks

    def fetch(self, timeout=20, user_agent=""):
        from newsroom.ingestion.adapters.base import FetchResult
        if self.marks is not None:
            self.marks.append(f"fetch-start:{self.name}")
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        if self.marks is not None:
            self.marks.append(f"fetch-end:{self.name}")
        return FetchResult(feed_url=self.name, retrieved_at="t",
                           payloads=list(self.payloads))

    def split_items(self, body):
        return [body]


def _patch_sources(monkeypatch, polling, adapters):
    sources = [(a.name, {"kind": "x", "_adapter": a}) for a in adapters]
    monkeypatch.setattr(polling, "enabled_sources", lambda s: sources)
    by_name = {a.name: a for a in adapters}
    monkeypatch.setattr(
        polling, "adapter_for", lambda kind, name, cfg: by_name[name])
    monkeypatch.setattr(polling, "_consecutive_failures", {})
    monkeypatch.setattr(polling, "_last_attempt", {})
    monkeypatch.setattr(polling, "_last_successful_poll", {})


def _settings(tmp_path):
    from newsroom.config import Settings
    return Settings({"polling": {"timeout_seconds": 5}}, tmp_path)


def test_failed_source_backs_off_and_recovers(monkeypatch, tmp_path):
    from newsroom.ingestion import polling
    boom = _Adapter(error=RuntimeError("down"))
    _patch_sources(monkeypatch, polling, [boom])
    ingest = lambda *a, **k: {}

    rep1 = polling.poll_once(None, _settings(tmp_path), ingest)
    rep2 = polling.poll_once(None, _settings(tmp_path), ingest)
    assert rep1["sources"]["fake"]["ok"] is False
    assert rep2["sources"]["fake"]["skipped"] == "backoff"
    assert rep2["sources"]["fake"]["fails"] == 1

    # Source recovers -> next allowed attempt succeeds, failure state resets.
    boom.error = None
    polling._consecutive_failures.clear()
    polling._last_attempt.clear()
    rep3 = polling.poll_once(None, _settings(tmp_path), ingest)
    assert rep3["sources"]["fake"]["ok"] is True
    assert "fake" not in polling._consecutive_failures


def test_fetch_is_concurrent_ingest_stays_config_ordered(monkeypatch, tmp_path):
    from newsroom.ingestion import polling
    marks: list[str] = []
    slow = _Adapter("slow", delay=0.4, marks=marks)
    fast = _Adapter("fast", delay=0.0, marks=marks)
    _patch_sources(monkeypatch, polling, [slow, fast])

    ingested = []
    polling.poll_once(
        None, _settings(tmp_path),
        lambda c, a, item, fu, ra: ingested.append(item) or
        {"new_observation": 1, "new_revision": 0})

    # The fast fetch finished while the slow one was still in flight —
    # proof they ran concurrently — yet ingest order follows config order.
    assert marks.index("fetch-end:fast") < marks.index("fetch-end:slow")
    assert ingested == ["i1", "i1"]


# --------------------------------------------------------- kokoro worker

def test_kokoro_engine_falls_back_to_cli_when_worker_fails(
        monkeypatch, tmp_path):
    from newsroom.audio import tts
    from newsroom.config import Settings
    settings = Settings({"tts": {"kokoro_model": "test-model"}},
                        tmp_path)
    eng = tts.KokoroMlxEngine(settings)

    def _dead_worker(model):
        class W:
            def synthesize(self, *a):
                raise RuntimeError("worker died")
        return W()
    monkeypatch.setattr(tts, "_worker_for", _dead_worker)

    # Fallback writes a real wav the engine then picks up.
    import wave
    wav = tmp_path / "seg_00_000.wav"
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 22050)

    monkeypatch.setattr(
        tts.KokoroMlxEngine, "_synthesize_subprocess",
        staticmethod(lambda model, text, voice, out_path: [wav]))
    res = eng.synthesize("disp", "speech", "af_heart",
                         tmp_path / "seg_00")
    assert res.engine == "kokoro_mlx"
    assert res.duration_s == pytest.approx(1.0, abs=0.05)
    assert res.path == tmp_path / "seg_00.wav"


def test_worker_killed_after_unresponsive_read(monkeypatch):
    """A timed-out request would desync the stdio protocol — the worker
    must be killed so the next attempt respawns clean."""
    import subprocess as sp
    from newsroom.audio import tts

    real_popen = sp.Popen
    procs = []

    def fake_popen(cmd, **kw):
        script = (
            "import sys,time;print('{\"ready\": true}');sys.stdout.flush();"
            "time.sleep(999)")
        p = real_popen(
            ["python3", "-c", script],
            stdin=kw.get("stdin"), stdout=kw.get("stdout"),
            stderr=kw.get("stderr"), env=kw.get("env"), text=True)
        procs.append(p)
        return p
    monkeypatch.setattr(tts.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tts, "_READ_TIMEOUT_S", 0.5)

    w = tts._KokoroWorker("m")
    with pytest.raises(RuntimeError, match="unresponsive"):
        w.synthesize("text", "voice", Path("/tmp/x"))
    assert w._proc is None  # killed, not left mid-request


# ------------------------------------------------------- map browser reuse

def test_render_plan_closes_reusable_map_renderer(tmp_path):
    from newsroom.models.shot import Shot, VisualPlan
    from newsroom.visuals import renderer as vr

    class Cards:
        name = "cards"
        def render_shot(self, shot, plan, out_dir, ctx):
            p = out_dir / f"{shot.type}_{ctx['seq']:02d}.png"
            p.write_bytes(b"png")
            return p

    class Maps:
        name = "maplibre"
        closed = False
        calls = 0
        def render_shot(self, shot, plan, out_dir, ctx):
            self.calls += 1
            p = out_dir / f"{shot.type}_{ctx['seq']:02d}.png"
            p.write_bytes(b"png")
            return p
        def close(self):
            self.closed = True

    maps = Maps()
    plan = VisualPlan(event_id="e", revision=1, width=8, height=8, fps=30,
                      shots=[
                          Shot(type="locator_map", duration_s=2, params={}),
                          Shot(type="metric_grid", duration_s=2, params={}),
                          Shot(type="locator_map", duration_s=2, params={}),
                      ])
    paths = vr.render_plan(Cards(), plan, tmp_path, {}, map_renderer=maps)
    assert maps.calls == 2           # both map shots went to the map renderer
    assert maps.closed               # browser released with the plan (§22)
    assert len(paths) == 3
