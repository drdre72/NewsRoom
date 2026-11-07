# macOS Production Notes

Everything here assumes a dedicated Apple Silicon Mac running the newsroom
continuously. Verify the machine with `python main.py doctor` before doing
anything else.

## Baseline hardware / OS

| Requirement | Minimum | Notes |
|---|---|---|
| CPU | Apple Silicon (arm64) | M1 Pro or better recommended |
| RAM | 16 GB unified | Drives model choices (§5) |
| Disk | 100 GB free | Media cache is capped in `settings.yaml` |
| OS | macOS 13+ | Native arm64 — **no Docker, no Rosetta, no VMs** |

## Required software

| Component | Install | Production role |
|---|---|---|
| Python ≥ 3.11 (arm64) | `brew install python@3.12` | newsroom state |
| uv | `brew install uv` | env + deps (`uv sync`) |
| FFmpeg (arm64) | `brew install ffmpeg` | media composition, RTMP encode |
| Node.js (arm64) | `brew install node` | Playwright / map tooling |
| Ollama | `brew install ollama` | local LLM (`ollama serve` runs on demand) |
| whisper.cpp | `brew install whisper.cpp` | speech alignment (Metal) |
| Chromium | `uv run playwright install chromium` | deterministic frame rendering |
| maplibre-gl | `npm i` (repo package.json) | offline map rendering, pinned v4 |

`h264_videotoolbox` must appear in `ffmpeg -encoders`. If it does not, the
FFmpeg build is wrong — do not fall back to `libx264` for 24/7 operation
(§29).

Post-install model pulls (kept out of git — `models/` and HF caches):

```bash
ollama pull qwen3:8b            # or rely on an installed preferred model
# whisper model → path configured in settings.yaml alignment.model_path
curl -L -o models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Kokoro (`mlx-community/Kokoro-82M-bf16`) and the misaki spacy model
download automatically on first synthesis into the HF cache.

## Memory model (16 GB)

The design is serialized by intent (§4, §5):

- **Always-on** (< ~500 MB total): feed polling, SQLite, scoring, the
  Broadcast Director, the FFmpeg playout process.
- **On-demand**: LLM, TTS, whisper alignment, Playwright/Chromium, render
  workers. Each is launched for a job and released after it.

`config/settings.yaml` `workers:` defaults everything to concurrency 1.
Do not raise them on a 16 GB machine — a Chromium instance plus an 8B
model plus a TTS model is already most of unified memory.

## Power policy (§40)

The Mac must not sleep while broadcasting. We do **not** change power
settings silently — apply these yourself on the production machine:

```bash
sudo pmset -a sleep 0          # never system-sleep on AC
sudo pmset -a displaysleep 10  # display may sleep; the stream doesn't care
sudo pmset -a autorestart 1    # reboot after power failure
sudo pmset -a powernap 0
```

Production machine must run on AC power. While the broadcast service is
active we additionally hold a `caffeinate -s` assertion from the playout
process — that prevents sleep without touching your `pmset` config.

Verify with `pmset -g assertions` while live: you should see
`PreventSystemSleep` held by the newsroom playout.

## launchd services

Repository-managed plists live in `src/newsroom/macos/launchd/` — templates
with `@TOKENS@` substituted at install time (never hand-edit the installed
copies; rerun install):

- `com.newsroom.core` — `main.py run --poll`: ingestion, intelligence,
  render-queue drain (`RunAtLoad`, `KeepAlive` unconditional — even a clean
  SIGTERM respawns; these are always-on services)
- `com.newsroom.broadcast` — `main.py broadcast`: the Director + HLS playout
  writing `output/preview/live.m3u8`. Each program airs inside a rendered
  16:9 channel frame (branding, impact chip, provenance, ticker); an RTMP
  second-output path exists but is gated by BOTH `outputs.rtmp.enabled`
  and `publishing.automatic` — currently false, nothing leaves the machine
- `com.newsroom.health` — `main.py preview`: HTTP server on :8788 serving
  the HLS preview, `/status.json` health, and the air log

Manage them through the CLI (works without an editable install):

```bash
python main.py services install            # renders + bootstraps all three
python main.py services install --print    # preview the rendered plists
python main.py services status
python main.py services uninstall
```

Logs land in `logs/` (JSON lines) plus per-service stdout/stderr
(`logs/{core,broadcast,preview}.{out,err}.log`). Single-director safety:
the broadcast process holds an `flock` on `output/preview/director.lock`;
a second `main.py broadcast` exits immediately rather than corrupt the
playlist.

### Local preview

Open **http://localhost:8788/** in Safari (HLS plays natively; no JavaScript
player needed). The same mux can later feed YouTube via FFmpeg's second
RTMP output — what you watch locally is what would stream. Enabling RTMP
requires `outputs.rtmp.enabled: true` in `config/broadcast.yaml`, the push
URL (with stream key) in `NEWSROOM_RTMP_URL` env — never in config/git —
AND `publishing.automatic: true`. All three are off by default (§66, §77).

### Sources

- **USGS** significant-hour GeoJSON — earthquakes (polled continuously)
- **NWS** `/alerts/active` — weather alerts (continuous)
- **NASA EONET** `/api/v3/events` — curated natural events: wildfire,
  volcano, severe storm, flood, landslide, snow, drought, sea/lake ice,
  temperature extremes (`poll_every_seconds: 3600`). Earthquake and
  non-newsworthy categories are skipped — USGS/NWS own those domains.
- **GDACS** RSS — UN/EU global disaster alerts (earthquake, cyclone,
  flood, volcano, drought, wildfire, tsunami) with Red/Orange/Green alert
  levels + population exposure (`poll_every_seconds: 1800`). GDACS
  earthquakes FUSE with USGS events for corroboration.
- **NOAA SWPC** `alerts.json` — space weather: geomagnetic storms (G1-G5),
  radiation storms (S1-S5), radio blackouts (R1-R5), K-index crossings
  (`poll_every_seconds: 900`). Repeated serials of the same message code
  revise one event; new thresholds escalate to new events.
- **ACLED** `acled/read` — curated conflict + protest events with actors,
  geography, and fatality counts (`poll_every_seconds: 3600`). Requires
  `ACLED_API_KEY` + `ACLED_EMAIL` env vars (free registration at
  developer.acleddata.com); the source reports `skipped: credentials`
  while unset — no error spam.
- **GDELT** Events v2 export — global media event database, CAMEO-coded
  conflict rows with actors, Goldstein scale, and source counts
  (`poll_every_seconds: 1800`, ~10MB zip). Shares `conflict_event` with
  ACLED so the two fuse and corroborate; fatality claims cite the §15
  `fatalities` fact explicitly.
- **FRED** `/fred/series/observations` — macro indicators (UNRATE,
  CPIAUCSL, PAYEMS, FEDFUNDS, GS10) as `economic_indicator` events; each
  new print revises the series event (`FRED_API_KEY` required).
- **BLS** v2 `/publicAPI/v2/timeseries/data/` — labor prints as
  `economic_indicator` (unkeyed at 25 req/day; `BLS_API_KEY` optional).
- **EIA** `/v2/seriesid/` — weekly energy inventories (crude excl-SPR,
  gasoline, distillate, L48 gas storage) as `economic_indicator`
  (`EIA_API_KEY` required). Surprise severity = |pct change|/move_scale.
- **FEMA** OpenFEMA `DisasterDeclarationsSummaries` — US disaster
  declarations (DR/EM/FM) as `disaster_declaration` events, one event per
  femaDeclarationString with designated counties merged (no key).
- **CISA KEV** known-exploited-vulnerabilities.json — actively exploited
  CVEs as `cyber_advisory` events; `lookback_days: 2` means only new
  catalog additions emit (no key).
- **Treasury FiscalData** monthly average rates on Treasury Bills, Notes,
  Bonds and TIPS, as stable `economic_indicator` series (no key). These are
  monthly historical averages, **not** live market yields.
- **NOAA NHC** `CurrentStorms.json` — active storms, wind speeds and
  advisory revisions (no key). Shares `tropical_cyclone` with GDACS;
  cross-source fusion requires matching time and position.
- **Coinbase** spot snapshots for BTC-USD and ETH-USD (no key). Each
  `market_quote` revision cites the exchange's price and compares it only
  to the preceding stored quote; the feed gives no exchange timestamp.
  This is exchange-specific crypto context, not licensed consolidated
  real-time market data. `markets` joins the severity-first rundown;
  Coinbase confidence is below the BREAKING interrupt gate.

The poller remembers successful fetches in process (including empty or
unchanged feeds) so source intervals remain enforced without new evidence;
it retries after a failed fetch. Launchd restarts reset that memory.
Automatic YouTube publishing and RTMP remain disabled.

### Rundown ordering

Air order is severity-first across all segments: FLASH cuts into live
programming, BREAKING preempts at boundaries, and the rundown picks the
highest-level + highest-score eligible event regardless of which desk it
belongs to. Segment priority only decides the label a story airs under.

When the rundown is empty the Director runs a replay rotation instead of
idling on the standby slate: stories whose current revision already
premiered re-air least-recently-aired first (`replay.enabled` in
`config/broadcast.yaml`). Replays are filler — any interrupt or fresh
rundown item preempts them mid-air, replay airings never count toward
segment cadence, and the frame shows a REPLAY badge rather than LIVE so
stale content is never presented as live. The standby slate only runs
when nothing airable exists, and it yields within a poll interval once a
render completes rather than waiting out its duration.

### Long-form explainer

`python main.py render-long <event_id>` produces a 1920×1080 horizontal
multi-chapter explainer — the YouTube main-feed leg. `LONGFORM_RENDER` is
a first-class job type (priority 70); the service loop drains it
serially alongside shorts. The script is a deeper treatment of the same
event: Overview → What We Know → Location → Timeline → How the Story
Evolved → Source Corroboration → Impact Assessment → Background → What
to Watch → Closing. Each chapter carries a YouTube chapter marker
(`youtube_chapters` in the manifest) ready for paste into the video
description. Background context about an event type (not the event
itself) is exempt from the per-segment citation requirement — it makes
no event-specific factual claim.

### On-air voice

Narration is locked to Kokoro MLX `af_heart`
(`tts.backend_order: [kokoro_mlx]`, `tts.strict_voice: true`): no `say`
fallback on air — if synthesis fails the render fails rather than ship an
unapproved voice. Add voices to `backend_order` only after a listen test.

## Boot recovery (§39)

After a restart: launchd starts `com.newsroom.core` → DB integrity check
runs → `running` jobs are re-queued → event state is already on disk →
broadcast resumes when the broadcast service is enabled. RAM holds no
persistent state.

## Network failure (§74)

Source polls that fail mark feed freshness and keep the last verified
state; the newsroom does not fabricate updates during an outage.

## Security notes

- Secrets live in `.env` (gitignored) or later macOS Keychain — never in
  `config/`, never committed (§77).
- Feed content is untrusted: it is parsed, never shell-executed (§76).
