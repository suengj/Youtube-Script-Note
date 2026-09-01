# YouTube Cookie File Setup

## Overview

A YouTube session cookie file lets yt-dlp act as a logged-in user and reduces HTTP 403 errors. Cookie files are **local-only credentials** — never commit them to Git.

## Setup

1. Install a browser extension such as **Get cookies.txt LOCALLY** (Chrome).
2. Log in to YouTube in your browser.
3. Export cookies for `youtube.com` to a local file (e.g. `youtube.com_cookies.txt`).
4. Copy the file into your project directory (optional):

```bash
cd "$PROJECT_ROOT"
cp ~/Downloads/youtube.com_cookies.txt ./youtube_cookies.txt
chmod 600 ./youtube_cookies.txt
```

5. Add to `.env`:

```env
YOUTUBE_COOKIES_FILE="${PROJECT_ROOT}/youtube_cookies.txt"
```

Use an absolute path if the file lives outside the repository.

## File permissions

Cookie files contain session credentials. Restrict access to your user account only:

```bash
chmod 600 /path/to/youtube_cookies.txt
```

Do **not** use world-readable permissions (`644` or `755`).

## Security

- Never commit cookie files. This repository ignores common variations:
  - `youtube_cookies.txt`
  - `youtube.com_cookies.txt`
  - `cookies.txt`
  - `*.cookies.txt`
- Rotate cookies periodically (every 1–2 weeks) or when 403 errors return.
- If a cookie file was ever committed, revoke the session (log out / change password) and rotate.

## Test

```bash
yt-dlp --cookies "$YOUTUBE_COOKIES_FILE" \
  --extract-audio --audio-format m4a \
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

## Troubleshooting

| Issue | Action |
|-------|--------|
| File not found | Verify `YOUTUBE_COOKIES_FILE` path in `.env` |
| 403 persists | Re-export cookies; confirm YouTube login; update yt-dlp |
| Extension fails | Try an alternate cookies.txt exporter |
