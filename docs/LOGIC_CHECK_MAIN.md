# Logic check: main.py (post subs-optimization)

Date: 2026-01-28  
Scope: `process_single_video` and related flow after YouTube subtitle-based pipeline changes.

---

## 1. Step 1 – Download and 7-tuple

| Check | Status | Notes |
|-------|--------|--------|
| `yt_downloader(..., config=config)` called with config | OK | main passes full config; stt uses `BASE_PATH`, `YT_DOWNLOAD_IF_SUBS_Y` |
| Unpack 7-tuple: `audio_path_file, audio_nm, video_id, video_len, channel_id, channel_url, subs_path` | OK | Single unpack line; stt always returns 7-tuple (ytdlp or pytubefix with `(*result, None)`) |
| `download_result is None` → return `download_failed` | OK | Before unpack |
| `download_result[0] == "__LIVE_SCHEDULED__"` → return `live_scheduled` | OK | No retry; queue marked done |
| `download_result[0] == "__VIDEO_UNAVAILABLE__"` → return `video_unavailable` | OK | Private/unavailable; no retry; queue marked done |
| `download_triggered = (audio_path_file is not None)` | OK | True when video downloaded, False when subs-only |
| Log DOWNLOAD_TRIGGERED, subs usage, STEP 1 message | OK | Correct branching message |
| Step 1 exception → return `download_failed`, video_id if set | OK | Outer try/except around Step 1 |

**Pytubefix fallback:** `yt_downloader` returns `(*result, None)` when yt-dlp unavailable → 7-tuple preserved. No unpack error.

---

## 2. Step 2 – Already processed

| Check | Status | Notes |
|-------|--------|--------|
| Uses `video_id` from Step 1 | OK | Always set when `download_result` is not None (from extract_info or pytubefix) |
| Skip when `video_id in output_df['v_id']` → return `already_existed` | OK | Unchanged |

---

## 3. Step 3 – Transcription source (subs vs Whisper)

| Check | Status | Notes |
|-------|--------|--------|
| Branch on `subs_path` (truthy) first | OK | If subs path present, use subs regardless of `audio_path_file` |
| **Subs branch:** `subtitle_file_to_plain_text(subs_path)` → `transcription`, `txt_file_name = f"{base_name}_{video_id}_{lang}_subs.txt"` | OK | base_name = sanitized title + `_` + video_id (VID always included); sets `transcription_length`; exception → `mlx_error` with SUBTITLE_READ_ERROR. Auto-subs: 주 언어 1개만 다운로드 (AUTO_SUBS_SINGLE_LANG_PLAN) |
| **Subs branch:** no use of `audio_path_file` | OK | Correct; when subs-only, `audio_path_file` is None |
| **Whisper branch:** only when `subs_path` is falsy | OK | Then `audio_path_file` is always set (video was downloaded) |
| **Whisper branch:** `transcribe_by_mlx(full_load_path=audio_path_file, ...)` | OK | Safe: this branch only runs when video was downloaded |
| **Whisper branch:** size_threshold return → `oversized_file` | OK | Before assigning `transcription` |
| **Whisper branch:** `rst is None` → `mlx_error` | OK | |
| **Whisper branch:** `transcription, txt_file_name = rst` and `transcription_length` | OK | |
| **Whisper branch:** FileNotFoundError / Exception handlers | OK | Both return `mlx_error`; indentation of the `except Exception` block is correct (16 spaces) |
| After Step 3, `transcription`, `txt_file_name`, `transcription_length` always set for success path | OK | Both branches set them; any failure returns before Step 4 |

---

## 4. Steps 4–7 (downstream)

| Check | Status | Notes |
|-------|--------|--------|
| Step 4 uses `transcription` and `transcription_length` | OK | Same variable names for both subs and Whisper |
| Step 5: `output_file = stt.change_filename(txt_file_name, "_5-mini")` | OK | e.g. `abc123_subs.txt` → `abc123_subs_5-mini.txt` |
| Step 6 (prompt_structure) uses `filename=audio_nm` | OK | When subs-only, `audio_nm` is placeholder (title or video_id); plan allows this |
| Step 7 (summary) uses `concise_transcription`, `audio_nm` | OK | Same as above |
| Step 8 (markdown save) uses `txt_file_name` → `change_filename` + `change_extension` → `.md` | OK | Naming consistent with Step 5 |
| No reference to `audio_path_file` after Step 3 in success path | OK | Only used in Step 3 Whisper branch and in error logs |

---

## 5. Directory creation and config

| Check | Status | Notes |
|-------|--------|--------|
| `yt_subs_path = os.path.join(base_path, 'yt_subs')` and in `directories_to_create` | OK | Created before any download |
| `config` passed to `process_single_video` and into `yt_downloader` | OK | Required for BASE_PATH and YT_DOWNLOAD_IF_SUBS_Y |

---

## 6. Edge cases and robustness

| Case | Status | Notes |
|------|--------|--------|
| Uploader subs exist, `YT_DOWNLOAD_IF_SUBS_Y=False`, subs download fails (stt) | OK | stt sets `has_uploader_subs = False`, downloads video, returns `subs_path=None` → main uses Whisper |
| Uploader subs exist, `YT_DOWNLOAD_IF_SUBS_Y=True` | OK | Video + subs downloaded; main gets `subs_path` set → uses subs, does not call Whisper |
| No uploader subs | OK | `subs_path=None`, Whisper branch, same as before |
| Subs path set but file read fails (e.g. missing/corrupt file) | OK | Step 3 subs branch try/except → return `mlx_error` with SUBTITLE_READ_ERROR |
| `config.py` load failure | OK | main fallback dict does not include `YT_DOWNLOAD_IF_SUBS_Y`; stt uses `config.get("YT_DOWNLOAD_IF_SUBS_Y", True)` → default True. Optional: add `YT_DOWNLOAD_IF_SUBS_Y` to main’s load_config fallback for consistency. |
| `base_path` missing (e.g. config without BASE_PATH) with uploader subs + `YT_DOWNLOAD_IF_SUBS_Y=False` | Edge | stt would return `(None, placeholder, video_id, ..., None)`; main would have `audio_path_file=None`, `subs_path=None` and enter Whisper branch → transcribe_by_mlx(None) would fail. In practice main always supplies BASE_PATH from load_config(), so safe. |

---

## 7. Optional / consistency notes (non-blocking)

1. **Full transcription when using subs:** For subs path, full text is written to `output_new/full/` when `subs_source != "auto"` or `SAVE_FULL_WHEN_AUTO_SUBS=True`. When `subs_source == "auto"` and `SAVE_FULL_WHEN_AUTO_SUBS=False` (default), full save is skipped.
2. **Step 1 log "Audio file: {audio_nm}":** When subs-only, `audio_nm` is a placeholder (title or video_id). Log is slightly misleading but acceptable; could be clarified as "Source label: {audio_nm}" when `download_triggered` is False.
3. **Empty subs file:** If `subtitle_file_to_plain_text` returns `""`, `transcription_length == 0`; flow continues to token_minimizer. No special handling; add later if needed.

---

## 8. Summary

- **7-tuple:** Unpacking and usage are correct; pytubefix path returns 7-tuple.
- **Subs vs Whisper:** Branching on `subs_path` is correct; no use of `audio_path_file` in subs branch; Whisper branch only runs when video was downloaded.
- **Steps 4–7:** Use common `transcription`, `txt_file_name`, `audio_nm`; naming and paths are consistent.
- **Logging:** DOWNLOAD_TRIGGERED, WHISPER_USED, and subs-usage message are present and correct.
- **Exception handling:** Step 3 subs and Whisper exception blocks (including `except Exception` indentation) are correct.
- **Edge cases:** Subs download failure fallback, config default, and BASE_PATH dependency are understood; only theoretical edge when BASE_PATH is missing.

**Conclusion:** Logic is consistent and matches the planned behavior. No code changes required for correctness; optional items above are for consistency or future improvement.
