# p03_speech2text

YouTube discovery → subtitles or audio → Whisper MLX fallback → LLM transcript preprocessing → structured Markdown summarization → optional Google Drive Desktop synchronization.

**Version:** 4.2.0

## What it does

P03 is a macOS / Apple Silicon pipeline that:

1. Discovers videos via channel crawl (YouTube Data API) or a local input queue
2. Downloads subtitles or audio (yt-dlp)
3. Falls back to on-device Whisper MLX when subtitles are unavailable
4. Preprocesses transcripts with a lightweight LLM (GPT-5-nano)
5. Summarizes into mobile-friendly Markdown (GPT-5-mini, INPUT_PROMPT v3)
6. Optionally syncs finalized Markdown to a Google Drive Desktop folder (`YT_summary`)

## Architecture

| Stage | Module | Notes |
|-------|--------|-------|
| Orchestration | `main.py` | 2-worker pool, single-writer shared state |
| Download / STT | `stt_function_v3.py` | yt-dlp, MLX Whisper, subtitle lifecycle |
| Channel crawl | `channel_crawl.py` | YouTube Data API v3 |
| Job workspace | `job_workspace.py`, `transcript_cache.py` | Per-video temp dirs, transcript cache |
| Drive sync | `scripts/drive_yt_summary/` | Filesystem transport via Google Drive Desktop |
| Config | `config.py`, `.env` | Non-secret defaults in `config.py`; secrets in `.env` |

See [docs/PROJECT.md](docs/PROJECT.md) for full pipeline documentation.

## Platform support

- **Supported:** macOS with Apple Silicon (MLX Whisper)
- **Not supported:** Windows / Linux (MLX dependency)

## Quick start

```bash
git clone https://github.com/suengj/p03_speech2text.git
cd p03_speech2text
cp .env.example .env   # fill API keys and paths
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Configuration

Copy `.env.example` to `.env` and set:

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Preprocess + summarize LLMs |
| `YOUTUBE_API_KEY` | Channel crawl (`CHANNEL_CRAWL=true`) |
| `OUTPUT_MD_PATH` | Markdown output directory |
| `HF_HOME` | Whisper MLX model cache |
| `BASE_PATH` / `WORK_PATH` / `DATA_ROOT` | Runtime layout (default: repo root + `data/`) |

Optional: `OPENROUTER_API_KEY`, `YOUTUBE_COOKIES_FILE`, `P03_DRIVE_SYNC_ROOT`. See `.env.example`.

## Runtime data (not in Git)

The following stay **local-only** and are listed in `.gitignore`:

- `.env`, cookie/session files
- `data/` — CSV queues, crawl state, metadata
- `audio/`, `tmp/`, `cache/`, `logs/`, `output_new/`
- Generated transcripts and summaries

## Security

API keys, OAuth tokens, and YouTube session cookies are **never** committed. See [SECURITY.md](SECURITY.md) and [docs/COOKIES_SETUP.md](docs/COOKIES_SETUP.md).

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -q
```

Offline/unit tests do not require API keys or personal paths.

## Optional Drive Desktop sync

When `P03_DRIVE_SYNC_ENABLED=1` and `P03_DRIVE_SYNC_ROOT` points to a local Google Drive Desktop `YT_summary` folder, finalized Markdown is copied to `source/` with an idempotent manifest. See [docs/SUE-401-DRIVE-YT-SUMMARY-SYNC.md](docs/SUE-401-DRIVE-YT-SUMMARY-SYNC.md).

## License / reuse

Source is published for reference. **No license grant is included** unless a `LICENSE` file is added by the owner. Do not assume permission to copy, modify, or redistribute beyond what copyright law allows.

## Docs

- [docs/PROJECT.md](docs/PROJECT.md) — pipeline reference
- [docs/YOUTUBE_API_SETUP.md](docs/YOUTUBE_API_SETUP.md) — API key setup
- [docs/LAUNCHD.md](docs/LAUNCHD.md) — macOS scheduling
- [docs/PUBLIC_RELEASE_AUDIT.md](docs/PUBLIC_RELEASE_AUDIT.md) — public release audit summary
