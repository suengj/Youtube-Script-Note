# Youtube Script Note (P03)

YouTube discovery → YouTube automatic captions → LLM transcript preprocessing → structured Markdown summary → verified Google Drive API upload (Linux) or optional Google Drive Desktop synchronization (macOS).

**Version:** 4.2.0

## What it does

P03 is a macOS / Apple Silicon pipeline that:

1. Discovers videos via channel crawl (YouTube Data API) or a local input queue
2. Downloads YouTube automatic captions only (yt-dlp)
3. Records `NO_AUTO_CAPTION` when automatic captions are unavailable; it never falls back to audio/ASR
4. Preprocesses transcripts with a lightweight LLM (GPT-5-nano)
5. Summarizes into mobile-friendly Markdown (v5.0 adds an opt-in single-pass GPT-6 Luna Responses path; legacy Nano + Mini remains the code default for rollback)

## Version 5.0

`LLM_PIPELINE_MODE=direct_luna` sends the raw transcript to one GPT-6 Luna Responses request. The default remains `legacy_two_stage` until owner approval. See [v5 migration notes](docs/MIGRATION_v5.md).
6. Uploads summary, cleaned transcript, and metadata to Google Drive with readback verification on Linux

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

Owner supplied individual video URLs can be imported into the existing `data/input_df.csv` queue. See [docs/MANUAL_VIDEO_INPUT.md](docs/MANUAL_VIDEO_INPUT.md).

## Platform support

- **Supported:** macOS and Linux (automatic captions only)

## Quick start

```bash
git clone https://github.com/suengj/Youtube-Script-Note.git
cd Youtube-Script-Note
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
| `BASE_PATH` / `WORK_PATH` / `DATA_ROOT` | Runtime layout (Linux default: `~/p03-run`) |
| `P03_DRIVE_BACKEND` | `api` on Linux (default) or `desktop` on macOS |
| `P03_DRIVE_TOKEN_FILE` | Google user OAuth token file locator for Drive API (Linux) |

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

## Drive API (Linux)

The Linux runner defaults to `P03_DRIVE_BACKEND=api`. It writes the summary into the existing `YT_summary/source` folder, creates/uses `YT_summary/transcripts/` for the transcript and metadata JSON, and only marks a video successful after Drive metadata readback matches the uploaded bytes. VTT files stay in each job workspace and are removed when verified processing completes. The systemd user units are in `deploy/systemd/`; install is an operator action.

Set `P03_DRIVE_TOKEN_FILE` to the user OAuth token file. The token is refreshed in memory and is not written back.

## Optional Drive Desktop sync

When `P03_DRIVE_SYNC_ENABLED=1` and `P03_DRIVE_SYNC_ROOT` points to a local Google Drive Desktop `YT_summary` folder, finalized Markdown is copied to `source/` with an idempotent manifest. See [docs/DRIVE_SYNC.md](docs/DRIVE_SYNC.md).

## License / reuse

Source is published for reference. **No license grant is included** unless a `LICENSE` file is added by the owner. Do not assume permission to copy, modify, or redistribute beyond what copyright law allows.

## Docs

- [docs/PROJECT.md](docs/PROJECT.md) — pipeline reference
- [docs/YOUTUBE_API_SETUP.md](docs/YOUTUBE_API_SETUP.md) — API key setup
- [docs/LAUNCHD.md](docs/LAUNCHD.md) — macOS scheduling
- [docs/DRIVE_SYNC.md](docs/DRIVE_SYNC.md) — optional Google Drive Desktop sync
