# newsroom

A **local-first, zero-recurring-cost autonomous newsroom** for Apple Silicon
macOS. It watches authoritative real-world sources, maintains an auditable
model of events and their revisions, explains them with fact-constrained
local AI, visualizes them geographically, and produces Shorts, long-form
video, and (eventually) a persistent YouTube Live stream.

> Sources establish facts. Code establishes state.
> The LLM explains state. The Broadcast Director decides what goes to air.

No Docker, no Rosetta, no CUDA, no VMs — native arm64 macOS only.

## Quick start

```bash
git clone <repo> && cd newsroom
uv sync --extra dev          # Python env (uv)

python main.py doctor        # verify the Mac environment
python main.py simulate earthquake_major            # end-to-end offline demo
python main.py simulate earthquake_revision_sequence # revision/fact-ledger demo
python main.py run --once    # poll real USGS feed once
python main.py status        # JSON newsroom status
```

Optional capability installs (pipeline degrades gracefully without them,
§37):

```bash
brew install ollama whisper-cpp   # LLM + alignment
ollama pull qwen3:8b              # or any preferred model in models.yaml
uv sync --extra tts --extra maps  # Kokoro MLX + Playwright
uv run playwright install chromium
```

## Layout

```
config/        all tunables — sources, thresholds, models, pronunciation
src/newsroom/  the package: ingestion/ models/ storage/ intelligence/
               story/ llm/ audio/ visuals/ editor/ broadcast/ macos/
               quality/ sim/
tests/         offline unit tests (no internet required)
output/        shorts/ longform/ broadcast/ manifests/
docs/MACOS_PRODUCTION.md   production-machine requirements
```

## The boundary that matters

The LLM never creates facts, changes canonical state, or decides what airs.
Every factual sentence in a script cites Fact IDs from the append-only
ledger, and a deterministic claim validator rejects unsupported output.
See the project spec for the full architecture.
