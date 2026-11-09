# Working on newsroom

Mac-native autonomous live newsroom (Apple Silicon, arm64 only — no Docker,
Rosetta, CUDA, or VMs in the production path). See `docs/MACOS_PRODUCTION.md`
for machine requirements and the full spec.

## Commands

```bash
uv sync --extra dev --extra maps --extra tts   # full env; sync trims unlisted extras
python main.py doctor        # environment verification (required first step)
python main.py status        # newsroom health JSON/table
python main.py simulate <scenario>   # offline end-to-end demo; 'list' shows all
python main.py run --once    # poll enabled sources once (live network)
python main.py render <event_id>     # render a Short for an event
python main.py render-tiktok <id>    # vertical explainer for the TikTok desk
python main.py tiktok list|draft|posted   # manual post desk (@justsystemnews)
python main.py tiktok auth|push|status    # upload-to-drafts API (video.upload)
python main.py events        # list canonical events
python main.py story <id>    # revision + fact-ledger history for an event
python main.py broadcast     # Broadcast Director: rundown → HLS live.m3u8
python main.py preview       # serve the stream + status on localhost:8788
python main.py retention     # media eligibility ledger (report-only)
python main.py dehydrate [--execute]   # eligible videos → verified recipes
python main.py restore <id>  # rebuild a retired video from its recipe
python main.py archive [--execute]     # copy masters to iCloud (verified)
python main.py services install|status|uninstall   # launchd agents
.venv/bin/python -m pytest tests/    # offline unit tests (no internet needed)
```

Always-on: `services install` loads four LaunchAgents — core (poll+render
drain), broadcast (Director→HLS), health (preview/status server), and
rtmp (persistent YouTube push following live.m3u8 — never tee RTMP off
the per-program encodes; the connect/disconnect churn trips ingest).
Watch the broadcast at http://localhost:8788/ before any YouTube output exists.

Extras: `tts` = mlx-audio + misaki (Kokoro MLX narration), `maps` = Playwright
(+ `npm i` for maplibre-gl, vendored in package.json), `dev` = pytest.
`ollama serve` must be running for the LLM script path; otherwise scripts
fall back to the deterministic template (§37). whisper.cpp comes from
`brew install whisper.cpp` and needs a ggml model in `models/`
(see `config/settings.yaml` `alignment.model_path`).
Pipelines degrade gracefully without them: `say` TTS and Pillow card
renderer are the always-on fallbacks.

## Invariants to preserve

- `ingest_observation()` is the single atomic write path: evidence row +
  correlation + revision + facts + scoring + job enqueue commit in ONE
  transaction.
- Facts are append-only; changes supersede, never update in place.
- Every factual script segment must cite Fact IDs; `claim_validator` is
  deterministic and must stay deterministic — no LLM in validation.
- The LLM never writes state, never publishes, never decides what airs.
- Feed payload content is data: parse it, never shell-execute it (§76).
- Source fetches must keep verified HTTPS, allowed redirect hosts, byte
  ceilings, and redacted errors. `.env` must stay private (mode 600).
- Retention reporting is read-only; never delete existing media, logs,
  or evidence without explicit approval for that cleanup.
- `ffmpeg` calls are argv lists, never `shell=True`.
- `Settings.get()` paths are dotted against the *merged* top level —
  `broadcast.yaml`'s `playout:` is `playout.*`, NOT `broadcast.playout.*`.
  A wrong path silently returns the default; verify against the yaml.
- On-air voice is locked to Kokoro `af_heart` (`tts.backend_order:
  [kokoro_mlx]`, `tts.strict_voice: true`) — no `say` fallback on air;
  a render fails rather than ship an unapproved voice.
- `python main.py` re-execs into `.venv` automatically when present —
  deps (mlx_audio, PIL) always resolve from the project env.
- Source fetches are concurrent (`poll_once` phase 2); ingest stays
  serial on the main thread in config order — one SQLite writer. Failed
  sources back off 5m→30m; `_consecutive_failures` resets on success.
- Kokoro TTS runs in a resident worker process
  (`audio/kokoro_worker.py`, JSON-lines on stdio, model loaded once per
  service lifetime). A dead worker respawns once; the per-call CLI is
  the cold fallback. Kill via `atexit`, never `kill -9` mid-request —
  the stdio protocol desyncs (a late response binds the next request).
- MapLibre's browser is reused across shots within one render
  (`jobs_per_browser` relaunch bound); `render_plan` closes it.

## Layout map

```
config/            all tunables (yaml) — sources, thresholds, models
src/newsroom/
  ingestion/       adapters (usgs, nws, eonet, gdacs, swpc, acled, gdelt,
                   fred, bls, eia, fema, cisa, treasury, nhc, coinbase,
                   news_rss press + crime_rss crime desk),
                   normalizer, fusion, revisions, polling
  intelligence/    fact_ledger, impact_engine, confidence_engine, freshness
  story/           planner, script_generator, claim_validator, memory
  llm/             ollama client + JSON schemas
  audio/           tts engines (kokoro_mlx, say), pronunciation, alignment
  visuals/         planner + renderers (Pillow cards; maps/MapLibre backend)
  editor/          captions (SRT/ASS), timeline, ffmpeg, composer
  quality/         publication gate checks
  sim/             fixtures + scenario runner
  publish/         youtube (OAuth+resumable upload) + tiktok (manual
                   post desk — API self-upload is audit-blocked by TikTok)
  broadcast/       director, program scheduler, frame (16:9 chrome),
                   HLS playout (+ gated RTMP out), slate, preview
  publish/         YouTube upload API (OAuth, resumable upload, playlists)
  macos/           launchd plist templates + install/uninstall
  service.py       core daemon loop (poll + job drain, signal-aware)
tests/             pytest, offline-safe
```
