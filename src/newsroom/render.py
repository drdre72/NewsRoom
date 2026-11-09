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
import os
import re
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


def _compose_atomic(timeline: Timeline, out: Path, settings: Settings,
                    ass_path: Path) -> None:
    """Encode to a sibling .part file, then os.replace — a reader (the
    playout) only ever sees a complete MP4, never one mid-write."""
    tmp = out.with_suffix(".part.mp4")
    try:
        compose_short(timeline, tmp, settings, ass_path=ass_path)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


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
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", event_id)
    work = cache_dir() / "renders" / f"{safe_id}_r{rev}"
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

    # --- narration + visuals: TTS is subprocess-bound and static shots
    # render identically regardless of final durations, so the two stages
    # overlap. Animated camera shots bake frame counts from duration_s —
    # for those, narration finishes first (serial path, §37-safe order).
    engine = select_engine(settings)
    voice = (settings.get("tts.kokoro_voice")
             if engine and engine.name == "kokoro_mlx" else
             settings.get("tts.say_voice"))

    def _narrate() -> tuple:
        narration = None
        narration_degraded = False
        caption_items: list[tuple[float, float, str]] = []
        seg_wavs: list[Path] = []
        if engine:
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
                                      engine=engine.name,
                                      fallback=candidate.name, segment=i)
                            narration_degraded = True
                        break
                    except Exception as exc:  # noqa: BLE001 — §37
                        log_event(log, 40, "segment tts failed",
                                  engine=candidate.name, error=str(exc),
                                  segment=i)
                if res is None:
                    if settings.get("tts.strict_voice"):
                        # Voice lock: never ship mixed/silent narration —
                        # fail the render so the job surfaces as failed (§37).
                        raise RuntimeError(
                            f"tts failed for segment {i} and strict_voice "
                            "forbids fallback; render aborted")
                    break
                seg_wavs.append(res.path)
                seg.duration_s = res.duration_s
                caption_items.append((t, t + res.duration_s, seg.text))
                t += res.duration_s + _GAP_S
            else:
                narration = _concat_audio(seg_wavs,
                                          audio_dir / "narration.wav")
            narration_dur = t - _TITLE_S - _GAP_S if seg_wavs else 0
        else:
            log_event(log, 40,
                      "no tts backend; producing silent visual bulletin")
            narration_dur = max(12.0, len(script.segments) * 4.0)
            t = _TITLE_S
            for seg in script.segments:
                est = max(3.0, len(seg.text.split()) * 0.42)
                seg.duration_s = est
                caption_items.append((t, t + est, seg.text))
                t += est + _GAP_S
        return narration, narration_degraded, caption_items, narration_dur

    # --- visuals --------------------------------------------------------
    vcfg = settings.get("video.shorts", {})
    w = int(vcfg.get("width", 1080)); h = int(vcfg.get("height", 1920))
    fps = int(vcfg.get("fps", 30))
    vplan = plan_visuals(conn, event, script, w, h, fps)

    card_renderer = CardRenderer()
    map_renderer = _select_map_renderer(settings)
    context = {
        "level": event.impact_level,
        "event_label": event.event_type.replace("_", " "),
        "source_line": f"SOURCE: {event.source_ids[0] if event.source_ids else 'unknown'}",
        "updated": f"UPDATED {event.last_updated[11:19]} UTC",
    }

    animated = any(s.type in ("globe_to_location", "impact_radius")
                   for s in vplan.shots)
    if animated:
        narration, narration_degraded, caption_items, narration_dur = _narrate()
        _fit_shots(vplan, narration_dur)
        shot_paths = render_plan(card_renderer, vplan, shots_dir, context,
                                 map_renderer=map_renderer)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            audio_fut = pool.submit(_narrate)
            shot_paths = render_plan(card_renderer, vplan, shots_dir, context,
                                     map_renderer=map_renderer)
            narration, narration_degraded, caption_items, narration_dur = (
                audio_fut.result())
        _fit_shots(vplan, narration_dur)

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
    out.parent.mkdir(parents=True, exist_ok=True)
    _compose_atomic(timeline, out, settings, ass_path=ass)

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
        "visual_plan": vplan.to_dict(),
        "captions": [[a, b, t] for a, b, t in caption_items],
        "render": {"title_s": _TITLE_S, "tail_s": _TAIL_S, "gap_s": _GAP_S,
                   "mid_min_s": 2.0, "target_floor_s": 4.0},
        "video": {"path": str(out), "hash": _sha256(out), "width": w,
                  "height": h, "fps": fps},
        "tts_model": engine.name if engine else None,
        "voice": voice if engine else None,
        "renderer": (f"{card_renderer.name}+{map_renderer.name}"
                     if map_renderer else card_renderer.name),
        "platform": "macos-arm64",
        "dry_run": dry_run,
    }
    mpath = output_dir() / "manifests" / f"{safe_id}_r{rev}.json"
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


def _fit_shots(vplan, narration_dur: float,
               mid_floor: float = 4.0, mid_min: float = 2.0) -> None:
    """Fit shots to narration: title fixed, tail fixed, middle scales."""
    middle = [s for s in vplan.shots
              if s.type not in ("title_card", "source_card")]
    title = next((s for s in vplan.shots if s.type == "title_card"), None)
    tail = next((s for s in vplan.shots if s.type == "source_card"), None)
    if title:
        title.duration_s = _TITLE_S
    if tail:
        tail.duration_s = _TAIL_S
    target_mid = max(mid_floor, narration_dur)
    cur_mid = sum(s.duration_s for s in middle) or 1.0
    for s in middle:
        s.duration_s = round(
            max(mid_min, s.duration_s * target_mid / cur_mid), 2)


def _tts_chain(engine, settings) -> list:
    """Preferred engine, then `say` — unless strict_voice locks narration
    to the approved voice (a failed render beats the wrong voice on air)."""
    from .audio.tts import SayEngine
    chain = [engine]
    if engine.name != "say" and not settings.get("tts.strict_voice"):
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


def render_longform(
    conn: sqlite3.Connection,
    settings: Settings,
    event_id: str,
    dry_run: bool = False,
    *,
    fmt: str = "longform",
) -> dict:
    """Multi-chapter explainer render — 1920x1080 (longform) or
    1080x1920 (tiktok).

    The same event gets a deeper treatment than the short: a multi-chapter
    script (lede → what we know → context → revisions → corroboration →
    impact → background → what to watch → close). ``fmt="longform"`` is the
    horizontal YouTube leg with chapter markers; ``fmt="tiktok"`` is the
    vertical explainer for the TikTok post desk — same fact ledger, same
    deterministic validation, same Kokoro voice lock.
    """
    from .story.story_planner import plan_longform
    from .story.script_generator import generate_longform_script

    event = repo.get_event(conn, event_id)
    if not event:
        raise ValueError(f"unknown event {event_id}")
    rev = event.current_revision
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", event_id)
    work_tag = "long" if fmt == "longform" else fmt
    work = cache_dir() / "renders" / f"{safe_id}_r{rev}_{work_tag}"
    shots_dir = work / "shots"
    audio_dir = work / "audio"
    shots_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    # --- language -----------------------------------------------------
    ctx = build_fact_context(conn, event_id)
    plan = plan_longform(conn, event_id)
    if not plan:
        raise ValueError(f"no longform plan for {event_id}")
    script, chapters = generate_longform_script(ctx, plan, settings, event)
    ok, errors = validate_script(script, ctx, settings)
    if not ok:
        raise RuntimeError(f"{fmt} script failed validation: {errors}")
    script.validated = True
    story_id = repo.new_id("story")
    repo.insert_story(conn, story_id, event_id, rev, json.dumps(plan.__dict__),
                      json.dumps(script.to_dict()), script.generator, True,
                      "validated")

    # --- narration (per-segment, same Kokoro pipeline) ----------------
    engine = select_engine(settings)
    if not engine:
        raise RuntimeError(f"{fmt} requires a TTS engine; none available")
    narration = None
    narration = None
    narration_degraded = False
    voice = (settings.get("tts.kokoro_voice")
             if engine.name == "kokoro_mlx" else
             settings.get("tts.say_voice"))

    def _narrate() -> tuple:
        caption_items: list[tuple[float, float, str]] = []
        seg_wavs: list[Path] = []
        degraded = False
        t = _TITLE_S
        for i, seg in enumerate(script.segments):
            wav = audio_dir / f"seg_{i:02d}.wav"
            res = None
            for candidate in _tts_chain(engine, settings):
                try:
                    res = candidate.synthesize(
                        seg.text, seg.speech_text,
                        voice or settings.get("tts.say_voice", ""), wav)
                    if candidate is not engine:
                        degraded = True
                    break
                except Exception as exc:  # noqa: BLE001
                    log_event(log, 40, f"{fmt} segment tts failed",
                              engine=candidate.name, error=str(exc),
                              segment=i)
            if res is None:
                if settings.get("tts.strict_voice"):
                    raise RuntimeError(
                        f"tts failed for segment {i} and strict_voice "
                        f"forbids fallback; {fmt} aborted")
                break
            seg_wavs.append(res.path)
            seg.duration_s = res.duration_s
            caption_items.append((t, t + res.duration_s, seg.text))
            t += res.duration_s + _GAP_S
        narration_path = (_concat_audio(seg_wavs, audio_dir / "narration.wav")
                          if seg_wavs else None)
        return (narration_path, degraded, caption_items,
                t - _TITLE_S - _GAP_S if seg_wavs else 0)

    # --- visuals ------------------------------------------------------
    vcfg = settings.get(f"video.{fmt}", {}) or {}
    if fmt == "longform":
        w = int(vcfg.get("width", 1920))
        h = int(vcfg.get("height", 1080))
    else:
        w = int(vcfg.get("width", 1080))
        h = int(vcfg.get("height", 1920))
    fps = int(vcfg.get("fps", 30))
    vplan = plan_visuals(conn, event, script, w, h, fps)

    card_renderer = CardRenderer()
    map_renderer = _select_map_renderer(settings)
    context = {
        "level": event.impact_level,
        "event_label": event.event_type.replace("_", " "),
        "source_line": f"SOURCE: {event.source_ids[0] if event.source_ids else 'unknown'}",
        "updated": f"UPDATED {event.last_updated[11:19]} UTC",
    }

    # Same overlap as render_short: static shots don't need final
    # durations; animated camera shots do (frame count = duration × fps).
    animated = any(s.type in ("globe_to_location", "impact_radius")
                   for s in vplan.shots)
    if animated:
        narration, narration_degraded, caption_items, narration_dur = _narrate()
        _fit_shots(vplan, narration_dur, mid_floor=8.0, mid_min=3.0)
        shot_paths = render_plan(card_renderer, vplan, shots_dir, context,
                                 map_renderer=map_renderer)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            audio_fut = pool.submit(_narrate)
            shot_paths = render_plan(card_renderer, vplan, shots_dir, context,
                                     map_renderer=map_renderer)
            narration, narration_degraded, caption_items, narration_dur = (
                audio_fut.result())
        _fit_shots(vplan, narration_dur, mid_floor=8.0, mid_min=3.0)

    # --- resolve chapter start times from caption timeline -----------
    # The script generator records each chapter's first_segment index;
    # we look up its caption start time directly.
    for ch in chapters:
        fi = ch.get("first_segment", 0)
        if fi < len(caption_items):
            ch["start_s"] = round(caption_items[fi][0], 2)
        elif caption_items:
            ch["start_s"] = round(caption_items[-1][1], 2)
        else:
            ch["start_s"] = _TITLE_S

    # --- captions + compose --------------------------------------------
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
    out = output_dir() / fmt / f"{slug}_r{rev}_{fmt}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    _compose_atomic(timeline, out, settings, ass_path=ass)

    # --- manifest + gate ----------------------------------------------
    manifest = {
        "event_id": event_id, "event_revision": rev,
        "rendered_at": repo.utcnow(),
        "story_id": story_id,
        "sources": event.source_ids,
        "fact_ids": [f.fact_id for f in ctx.current],
        "format": fmt,
        "chapters": [{"title": c["title"],
                      "start_s": c.get("start_s", 0.0)}
                     for c in chapters],
        "script": {"generator": script.generator, "validated": True,
                   "hash": _sha256(work / "captions.srt")},
        "audio": {"engine": engine.name,
                  "degraded": narration_degraded,
                  "hash": _sha256(narration) if narration else None},
        "visual_plan_hash": hashlib.sha256(
            json.dumps(vplan.to_dict(), sort_keys=True).encode()).hexdigest(),
        "visual_plan": vplan.to_dict(),
        "captions": [[a, b, t] for a, b, t in caption_items],
        "render": {"title_s": _TITLE_S, "tail_s": _TAIL_S, "gap_s": _GAP_S,
                   "mid_min_s": 3.0, "target_floor_s": 8.0},
        "video": {"path": str(out), "hash": _sha256(out), "width": w,
                  "height": h, "fps": fps,
                  "duration_s": round(timeline.video_duration(), 2)},
        "tts_model": engine.name,
        "voice": voice,
        "renderer": (f"{card_renderer.name}+{map_renderer.name}"
                     if map_renderer else card_renderer.name),
        "platform": "macos-arm64",
        "dry_run": dry_run,
    }
    if fmt == "longform":
        # YouTube-only marker syntax; TikTok drafts keep chapters as data.
        manifest["youtube_chapters"] = "\n".join(
            f"{int(c.get('start_s', 0))//60:02d}:"
            f"{int(c.get('start_s', 0))%60:02d} {c['title']}"
            for c in chapters)
    mpath = output_dir() / "manifests" / f"{safe_id}_r{rev}_{fmt}.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    for kind, p in (("video", out), ("manifest", mpath), ("caption", ass),
                    ("audio", narration)):
        if p:
            repo.insert_asset(conn, repo.new_id("asset"), kind, str(p),
                              _sha256(p), event_id, rev,
                              {"format": fmt})
    gate = gate_render(conn, settings, manifest)
    manifest["publication_gate"] = gate
    mpath.write_text(json.dumps(manifest, indent=2))
    log_event(log, 20, f"{fmt} render complete", event_id=event_id,
              revision=rev, video=str(out), gate=gate["verdict"],
              chapters=len(chapters),
              renderer=manifest["renderer"], tts=engine.name)
    return manifest


def render_tiktok(
    conn: sqlite3.Connection,
    settings: Settings,
    event_id: str,
    dry_run: bool = False,
) -> dict:
    """Vertical explainer for the TikTok post desk (@justsystemnews).

    Same multi-chapter explainer script as the YouTube long-form leg,
    rendered 1080x1920. Posting is manual (tiktok.com/upload) — the
    `tiktok draft` command packages this render for review and posting.
    """
    return render_longform(conn, settings, event_id, dry_run=dry_run,
                           fmt="tiktok")


def render_from_recipe(
    conn: sqlite3.Connection,
    settings: Settings,
    recipe: dict,
    out_path: Path | None = None,
    persist: bool = True,
) -> dict:
    """Rebuild a retired video from its rehydration recipe — no new
    editorial work: the validated script replays verbatim, the frozen
    visual plan re-fits to fresh narration, the same gate runs again.

    persist=False renders to the recipe's scratch dir for verification
    only (no asset rows, no broadcast-visible output).
    """
    from .models.event import CanonicalEvent
    from .models.shot import VisualPlan
    from .models.story import Script
    from .quality.visual import check_video

    event = CanonicalEvent(**recipe["event"])
    rev = int(recipe["revision"])
    fmt = recipe.get("format", "short")
    script = Script.from_dict(recipe["script"])
    script.validated = True          # stored script passed validation once
    vplan = VisualPlan.from_dict(recipe["visual_plan"])
    rcfg = recipe.get("render", {})
    title_s = float(rcfg.get("title_s", _TITLE_S))
    tail_s = float(rcfg.get("tail_s", _TAIL_S))
    gap_s = float(rcfg.get("gap_s", _GAP_S))
    mid_min = float(rcfg.get("mid_min_s", 3.0 if fmt == "longform" else 2.0))
    floor = float(rcfg.get("target_floor_s", 8.0 if fmt == "longform" else 4.0))
    w = int(rcfg.get("width", vplan.width))
    h = int(rcfg.get("height", vplan.height))
    fps = int(rcfg.get("fps", vplan.fps))

    rid = recipe.get("recipe_id", "recipe")
    work = cache_dir() / "renders" / f"restore_{rid}"
    shots_dir = work / "shots"
    audio_dir = work / "audio"
    shots_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    # --- narration: replay each stored segment through the pinned voice --
    engine = select_engine(settings)
    if not engine:
        raise RuntimeError("restore requires a TTS engine; none available")
    # The engine's configured voice wins — the recipe's recorded voice is
    # provenance only. strict_voice forbids anything but the approved
    # on-air voice, even when the retired original predates the lock.
    voice = (settings.get("tts.kokoro_voice")
             if engine.name == "kokoro_mlx"
             else settings.get("tts.say_voice"))
    if recipe.get("voice") and recipe["voice"] != voice:
        log_event(log, 20, "recipe voice substituted",
                  recorded=recipe["voice"], using=voice)
    caption_items: list[tuple[float, float, str]] = []
    seg_wavs: list[Path] = []
    t = title_s
    for i, seg in enumerate(script.segments):
        wav = audio_dir / f"seg_{i:02d}.wav"
        res = engine.synthesize(seg.text, seg.speech_text, voice, wav)
        seg.duration_s = res.duration_s
        seg_wavs.append(res.path)
        caption_items.append((t, t + res.duration_s, seg.text))
        t += res.duration_s + gap_s
    narration = _concat_audio(seg_wavs, audio_dir / "narration.wav",
                              gap_s=gap_s)
    narration_dur = t - title_s - gap_s if seg_wavs else 0

    # --- visuals: frozen plan, durations re-fit to the fresh narration --
    middle = [s for s in vplan.shots
              if s.type not in ("title_card", "source_card")]
    title = next((s for s in vplan.shots if s.type == "title_card"), None)
    tail = next((s for s in vplan.shots if s.type == "source_card"), None)
    if title:
        title.duration_s = title_s
    if tail:
        tail.duration_s = tail_s
    target_mid = max(floor, narration_dur)
    cur_mid = sum(s.duration_s for s in middle) or 1.0
    for s in middle:
        s.duration_s = round(max(mid_min, s.duration_s * target_mid
                                 / cur_mid), 2)

    card_renderer = CardRenderer()
    map_renderer = _select_map_renderer(settings)
    context = recipe.get("context") or {
        "level": event.impact_level,
        "event_label": event.event_type.replace("_", " "),
        "source_line": f"SOURCE: {event.source_ids[0] if event.source_ids else 'unknown'}",
        "updated": f"UPDATED {event.last_updated[11:19]} UTC",
    }
    shot_paths = render_plan(card_renderer, vplan, shots_dir, context,
                             map_renderer=map_renderer)

    # --- captions + compose --------------------------------------------
    ass = write_ass(caption_items, work / "captions.ass", w, h)
    write_srt(caption_items, work / "captions.srt")
    timeline = Timeline(width=w, height=h, fps=fps,
                        audio=narration, audio_delay_s=title_s,
                        caption_items=caption_items)
    for shot, path in zip(vplan.shots, shot_paths):
        timeline.visuals.append(VisualSegment(
            source=path, duration_s=shot.duration_s,
            is_sequence=path.is_dir()))

    out = out_path or Path(recipe["video"]["path"])
    if out.exists() and out_path is None:
        # Never clobber a file that isn't the retired original.
        out = out.with_name(f"{out.stem}_restored{out.suffix}")
        if out.exists():
            raise RuntimeError(f"restore target already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    _compose_atomic(timeline, out, settings, ass_path=ass)

    # --- chapters (longform): stored first-segment indices re-resolve ---
    chapters = script.extra.get("chapters") or []
    for ch in chapters:
        fi = ch.get("first_segment", 0)
        if fi < len(caption_items):
            ch["start_s"] = round(caption_items[fi][0], 2)
        elif caption_items:
            ch["start_s"] = round(caption_items[-1][1], 2)
        else:
            ch["start_s"] = title_s

    manifest = {
        "event_id": event.event_id, "event_revision": rev,
        "rendered_at": repo.utcnow(),
        "story_id": recipe.get("story_id"),
        "sources": recipe.get("sources", event.source_ids),
        "fact_ids": recipe.get("fact_ids", []),
        "format": fmt,
        "restored_from": rid,
        "script": {"generator": f"{script.generator}:recipe",
                   "validated": True,
                   "hash": _sha256(work / "captions.srt")},
        "audio": {"engine": engine.name, "degraded": False,
                  "hash": _sha256(narration)},
        "visual_plan_hash": hashlib.sha256(
            json.dumps(vplan.to_dict(), sort_keys=True).encode()).hexdigest(),
        "video": {"path": str(out), "hash": _sha256(out), "width": w,
                  "height": h, "fps": fps},
        "tts_model": engine.name,
        "voice": voice,
        "renderer": (f"{card_renderer.name}+{map_renderer.name}"
                     if map_renderer else card_renderer.name),
        "platform": "macos-arm64",
        "dry_run": False,
    }
    if chapters:
        manifest["chapters"] = [
            {"title": c["title"], "start_s": c.get("start_s", 0.0)}
            for c in chapters]
        manifest["youtube_chapters"] = "\n".join(
            f"{int(c.get('start_s', 0))//60:02d}:"
            f"{int(c.get('start_s', 0))%60:02d} {c['title']}"
            for c in chapters)

    if not persist:
        # Verification render: QC the file, keep everything in scratch.
        manifest["verify"] = check_video(manifest)
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return manifest

    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", event.event_id)
    suffix = "_longform" if fmt == "longform" else ""
    mpath = (output_dir() / "manifests"
             / f"{safe_id}_r{rev}{suffix}_restored.json")
    mpath.write_text(json.dumps(manifest, indent=2))
    meta = {"format": fmt, "restored_from": rid,
            "restored_at": repo.utcnow()}
    for kind, p in (("video", out), ("manifest", mpath), ("caption", ass),
                    ("audio", narration)):
        if p:
            repo.insert_asset(conn, repo.new_id("asset"), kind, str(p),
                              _sha256(p), event.event_id, rev,
                              meta if kind == "video" else {})
    gate = gate_render(conn, settings, manifest)
    manifest["publication_gate"] = gate
    mpath.write_text(json.dumps(manifest, indent=2))
    log_event(log, 20, "recipe restore complete", recipe_id=rid,
              event_id=event.event_id, revision=rev, video=str(out),
              gate=gate["verdict"])
    return manifest
