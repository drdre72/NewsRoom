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
python main.py events        # list canonical events
python main.py story <id>    # revision + fact-ledger history for an event
python main.py broadcast     # Broadcast Director: rundown → HLS live.m3u8
python main.py preview       # serve the stream + status on localhost:8788
python main.py services install|status|uninstall   # launchd agents
.venv/bin/python -m pytest tests/    # offline unit tests (no internet needed)
```

Always-on: `services install` loads three LaunchAgents — core (poll+render
drain), broadcast (Director→HLS), health (preview/status server). Watch
the broadcast at http://localhost:8788/ before any YouTube output exists.

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
- `ffmpeg` calls are argv lists, never `shell=True`.
- `Settings.get()` paths are dotted against the *merged* top level —
  `broadcast.yaml`'s `playout:` is `playout.*`, NOT `broadcast.playout.*`.
  A wrong path silently returns the default; verify against the yaml.

## Layout map

```
config/            all tunables (yaml) — sources, thresholds, models
src/newsroom/
  ingestion/       adapters (usgs, nws), normalizer, fusion, revisions, polling
  intelligence/    fact_ledger, impact_engine, confidence_engine, freshness
  story/           planner, script_generator, claim_validator, memory
  llm/             ollama client + JSON schemas
  audio/           tts engines (kokoro_mlx, say), pronunciation, alignment
  visuals/         planner + renderers (Pillow cards; maps/MapLibre backend)
  editor/          captions (SRT/ASS), timeline, ffmpeg, composer
  quality/         publication gate checks
  sim/             fixtures + scenario runner
  broadcast/       director, program scheduler, HLS playout, slate, preview
  macos/           launchd plist templates + install/uninstall
  service.py       core daemon loop (poll + job drain, signal-aware)
tests/             pytest, offline-safe
```
