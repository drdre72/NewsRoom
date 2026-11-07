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
| whisper.cpp | `brew install whisper-cpp` | speech alignment (Metal) |
| Chromium | `uv run playwright install chromium` | deterministic frame rendering |

`h264_videotoolbox` must appear in `ffmpeg -encoders`. If it does not, the
FFmpeg build is wrong — do not fall back to `libx264` for 24/7 operation
(§29).

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

## launchd services (Phase 11)

Repository-managed plists live in `src/newsroom/macos/launchd/`:

- `com.newsroom.core` — ingestion, intelligence, jobs (`RunAtLoad`, bounded `KeepAlive`)
- `com.newsroom.broadcast` — playout + encoder (Phase 10+)
- `com.newsroom.health` — status endpoint + supervisor

Install/uninstall:

```bash
python -m newsroom.macos.install_services
python -m newsroom.macos.uninstall_services
```

Logs land in `logs/` as JSON lines plus per-service stdout/stderr. Never
edit plists by hand — regenerate them.

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
