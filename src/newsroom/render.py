"""Shorts render pipeline — the first vertical slice (§60, §85).

    event -> fact context -> story plan -> script (llm|template) -> validate
    -> per-segment TTS -> alignment/timing -> visual plan -> shot renders
    -> captions -> ffmpeg compose -> manifest -> quality gate

Every artifact carries provenance. A failure at any stage marks the render
rejected — nothing here ever publishes automatically (§66).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from .audio.tts import select_engine
from .config import Settings
from .editor import ffmpeg
from .editor.captions import write_ass, write_srt
from .editor.composer import compose_short
from .editor.timeline import Timeline, VisualSegment
from .logging import get_logger, log_event
from .paths import cache_dir, output_dir
from .quality.publication_gate import gate_render
from .story.claim_validator import validate_script
from .story.context_builder import build_fact_context
from .story.script_generator import generate_script
from .story.story_planner import plan_story
from .storage import repositories as repo
from .visuals.planner import plan_visuals
from .visuals.renderer import CardRenderer, render_plan

log = get_logger("render")

_TITLE_S = 3.5
_TAIL_S = 4.0
_GAP_S = 0.35


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _concat_audio(paths: list[Path], out: Path, gap_s: float = _GAP_S) -> Path:
    """Concatenate segment wavs with gaps; returns narration wav path."""
    if len(paths) == 1:
        out.write_bytes(paths[0].read_bytes())
        return out
    n = len(paths)
    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    # One silence input per gap — each filtergraph pad is consumed once.
    for _ in range(n - 1):
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono"]
    chains: list[str] = []
    concat_in = ""
    for i in range(n):
        concat_in += f"[{i}:a]"
        if i < n - 1:
            g = f"g{i}"
            chains.append(f"[{n + i}:a]atrim=0:{gap_s}[{g}]")
            concat_in += f"[{g}]"
    chains.append(concat_in + f"concat=n={2 * n - 1}:v=0:a=1[a]")
    args = [*inputs, "-filter_complex", ";".join(chains), "-map", "[a]",
            "-ar", "22050", "-ac", "1", str(out)]
    ffmpeg.run(args)
    return out


def render_short(
    conn: sqlite3.Connection,
    settings: Settings,
    event_id: str,
    dry_run: bool = False,
) -> dict:
    event = repo.get_event(conn, event_id)
    if not event:
        raise ValueError(f"unknown event {event_id}")
    rev = event.current_revision
    work = cache_dir() / "renders" / f"{event_id.replace(':', '_')}_r{rev}"
    shots_dir = work / "shots"
    audio_dir = work / "audio"
    shots_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    # --- language -----------------------------------------------------
    ctx = build_fact_context(conn, event_id)
    plan = plan_story(conn, event_id)
    script = generate_script(ctx, plan, settings)
    ok, errors = validate_script(script, ctx, settings)
    if not ok and script.generator != "template":
        log_event(log, 40, "llm script rejected; regenerating via template",
                  event_id=event_id, errors=errors)
        from .story.script_generator import _script_from_template
        script = _script_from_template(ctx, plan, settings)
        script.generator += ":fallback"
        ok, errors = validate_script(script, ctx, settings)
    script.validated = ok
    script.validation_errors = errors
    if not ok:
        raise RuntimeError(f"script failed validation: {errors}")
    story_id = repo.new_id("story")
    repo.insert_story(conn, story_id, event_id, rev, json.dumps(plan.__dict__),
                      json.dumps(script.to_dict()), script.generator, True,
                      "validated")

    # --- narration (per-segment => real durations, honest captions) -----
    engine = select_engine(settings)
    narration = None
    narration_degraded = False
    caption_items: list[tuple[float, float, str]] = []
    seg_wavs: list[Path] = []
    if engine:
        voice = (settings.get("tts.kokoro_voice")
                 if engine.name == "kokoro_mlx" else
                 settings.get("tts.say_voice"))
        t = _TITLE_S
        for i, seg in enumerate(script.segments):
            wav = audio_dir / f"seg_{i:02d}.wav"
            res = None
            # Per-segment degrade: preferred engine, then `say` (§37).
            for candidate in _tts_chain(engine, settings):
                try:
                    res = candidate.synthesize(
                        seg.text, seg.speech_text, voice or
                        settings.get("tts.say_voice", ""), wav)
                    if candidate is not engine:
                        log_event(log, 30, "tts degraded",
                                  engine=engine.name, fallback=candidate.name,
                                  segment=i)
                        narration_degraded = True
                    break
                except Exception as exc:  # noqa: BLE001 — TTS failure policy §37
                    log_event(log, 40, "segment tts failed",
                              engine=candidate.name, error=str(exc),
                              segment=i)
            if res is None:
                break
            seg_wavs.append(res.path)
            seg.duration_s = res.duration_s
            caption_items.append((t, t + res.duration_s, seg.text))
            t += res.duration_s + _GAP_S
        else:
            narration = _concat_audio(seg_wavs, audio_dir / "narration.wav")
        narration_dur = t - _TITLE_S - _GAP_S if seg_wavs else 0
    else:
        log_event(log, 40, "no tts backend; producing silent visual bulletin")
        narration_dur = max(12.0, len(script.segments) * 4.0)
        t = _TITLE_S
        for seg in script.segments:
            est = max(3.0, len(seg.text.split()) * 0.42)
            seg.duration_s = est
            caption_items.append((t, t + est, seg.text))
            t += est + _GAP_S

    # --- visuals --------------------------------------------------------
    vcfg = settings.get("video.shorts", {})
    w = int(vcfg.get("width", 1080)); h = int(vcfg.get("height", 1920))
    fps = int(vcfg.get("fps", 30))
    vplan = plan_visuals(conn, event, script, w, h, fps)

    # Fit shots to narration: title fixed, tail fixed, middle scales.
    middle = [s for s in vplan.shots if s.type not in ("title_card", "source_card")]
    title = next((s for s in vplan.shots if s.type == "title_card"), None)
    tail = next((s for s in vplan.shots if s.type == "source_card"), None)
    if title:
        title.duration_s = _TITLE_S
    if tail:
        tail.duration_s = _TAIL_S
    target_mid = max(4.0, narration_dur)
    cur_mid = sum(s.duration_s for s in middle) or 1.0
    for s in middle:
        s.duration_s = round(max(2.0, s.duration_s * target_mid / cur_mid), 2)

    card_renderer = CardRenderer()
    map_renderer = _select_map_renderer(settings)
    context = {
        "level": event.impact_level,
        "event_label": event.event_type.replace("_", " "),
        "source_line": f"SOURCE: {event.source_ids[0] if event.source_ids else 'unknown'}",
        "updated": f"UPDATED {event.last_updated[11:19]} UTC",
    }
    shot_paths = render_plan(card_renderer, vplan, shots_dir, context,
                             map_renderer=map_renderer)

    # --- captions + compose ----------------------------------------------
    ass = write_ass(caption_items, work / "captions.ass", w, h)
    write_srt(caption_items, work / "captions.srt")
    timeline = Timeline(width=w, height=h, fps=fps,
                        audio=narration, audio_delay_s=_TITLE_S,
                        caption_items=caption_items)
    for shot, path in zip(vplan.shots, shot_paths):
        timeline.visuals.append(VisualSegment(
            source=path, duration_s=shot.duration_s,
            is_sequence=path.is_dir()))
    slug = event.location_label or event.event_id
    slug = "".join(c if c.isalnum() else "_" for c in slug)[:40].strip("_")
    out = output_dir() / "shorts" / f"{slug}_r{rev}_short.mp4"
    compose_short(timeline, out, settings, ass_path=ass)

    # --- manifest + gate ---------------------------------------------------
    manifest = {
        "event_id": event_id, "event_revision": rev,
        "rendered_at": repo.utcnow(),
        "story_id": story_id,
        "sources": event.source_ids,
        "fact_ids": [f.fact_id for f in ctx.current],
        "script": {"generator": script.generator, "validated": script.validated,
                   "hash": _sha256(work / "captions.srt")},
        "audio": {"engine": engine.name if engine else None,
                  "degraded": narration_degraded,
                  "hash": _sha256(narration) if narration else None},
        "visual_plan_hash": hashlib.sha256(
            json.dumps(vplan.to_dict(), sort_keys=True).encode()).hexdigest(),
        "video": {"path": str(out), "hash": _sha256(out), "width": w,
                  "height": h, "fps": fps},
        "tts_model": engine.name if engine else None,
        "renderer": (f"{card_renderer.name}+{map_renderer.name}"
                     if map_renderer else card_renderer.name),
        "platform": "macos-arm64",
        "dry_run": dry_run,
    }
    mpath = output_dir() / "manifests" / f"{event_id.replace(':', '_')}_r{rev}.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    for kind, p in (("video", out), ("manifest", mpath), ("caption", ass),
                    ("audio", narration)):
        if p:
            repo.insert_asset(conn, repo.new_id("asset"), kind, str(p),
                              _sha256(p), event_id, rev, {})
    gate = gate_render(conn, settings, manifest)
    manifest["publication_gate"] = gate
    mpath.write_text(json.dumps(manifest, indent=2))
    log_event(log, 20, "render complete", event_id=event_id, revision=rev,
              video=str(out), gate=gate["verdict"],
              renderer=manifest["renderer"],
              tts=engine.name if engine else None)
    return manifest


def _tts_chain(engine, settings) -> list:
    """Preferred engine, then `say` as the always-present safety net (§37)."""
    from .audio.tts import SayEngine
    chain = [engine]
    if engine.name != "say":
        chain.append(SayEngine(settings))
    return chain


def _select_map_renderer(settings: Settings):
    """MapLibre for geographic shots when installed; else None → cards (§37)."""
    try:
        from .visuals.maps.renderer import MapLibreRenderer
        ml = MapLibreRenderer()
        ok, _ = ml.available()
        if ok:
            return ml
    except Exception:  # noqa: BLE001
        pass
    return None
