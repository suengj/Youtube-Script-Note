#!/usr/bin/env python3
"""
M4A 무손실 아카이브 압축/복원 (zip_process.py)
config.py의 COMPRESSION_AUDIO_PATH(아카이브용 경로, 예: 별도 SSD) 내 M4A를
zstd/7z로 압축하거나 복원합니다. 원본은 검증 후에만 삭제합니다.

대략적인 소요 시간 (zstd level 19): 10MB M4A당 약 5~15초, 50MB당 약 20~60초.
7z는 압축이 더 느리고 복원은 zstd가 더 빠릅니다.
"""

import os
import sys
import json
import hashlib
import subprocess
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, desc="", unit="it", **kwargs):
        return iterable

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def load_compression_config() -> dict:
    """Load COMPRESSION_* from config.py (same package dir)."""
    defaults = {
        "COMPRESSION_AUDIO_PATH": "",
        "COMPRESSION_MODE": "zip",
        "COMPRESSION_METHOD": "zstd",
        "ZSTD_LEVEL": 19,
        "COMPRESSION_STATE_FILE": "compression_state.json",
        "COMPRESSION_RECURSIVE": False,
        "COMPRESSION_MIN_SIZE_MB": 10,
        "COMPRESSION_DELETE_AFTER_UNZIP": True,
    }
    try:
        from config import get_compression_config_dict
        return get_compression_config_dict()
    except Exception as e:
        print(f"Warning: config.py load failed ({e}), using defaults.", file=sys.stderr)
        return defaults


def sha256_file(path: str) -> str:
    """Return SHA256 hex digest of file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_meta_ffprobe(m4a_path: str) -> dict:
    """Get duration, sample_rate, channels, codec via ffprobe. Returns dict with 0/unknown on failure."""
    out = {
        "duration_sec": 0.0,
        "sample_rate": 0,
        "channels": 0,
        "codec": "unknown",
    }
    try:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            m4a_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return out
        info = json.loads(r.stdout)
        fmt = info.get("format") or {}
        streams = info.get("streams") or []
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        out["duration_sec"] = float(fmt.get("duration") or 0)
        out["sample_rate"] = int(audio.get("sample_rate") or 0)
        out["channels"] = int(audio.get("channels") or 0)
        out["codec"] = audio.get("codec_name") or "unknown"
    except Exception:
        pass
    return out


def collect_m4a_files(root: str, recursive: bool) -> List[str]:
    """Return list of absolute paths to .m4a files under root."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return []
    out = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                if f.lower().endswith(".m4a"):
                    out.append(os.path.join(dirpath, f))
    else:
        for f in os.listdir(root):
            if f.lower().endswith(".m4a"):
                out.append(os.path.join(root, f))
    return sorted(out)


def compress_zstd(src: str, dst: str, level: int) -> bool:
    """Compress src to dst with zstd. dst should be path to .zst file. Returns True on success."""
    try:
        subprocess.run(
            ["zstd", f"-{level}", "-f", "-o", dst, src],
            check=True,
            capture_output=True,
            timeout=3600,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def decompress_zstd(src_zst: str, dst: str) -> bool:
    """Decompress src_zst to dst. Returns True on success."""
    try:
        subprocess.run(
            ["zstd", "-d", "-f", "-o", dst, src_zst],
            check=True,
            capture_output=True,
            timeout=3600,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def compress_7z(src: str, dst_7z: str) -> bool:
    """Compress src to dst_7z (e.g. path.m4a.7z) with 7z LZMA2. Returns True on success."""
    try:
        subprocess.run(
            ["7z", "a", "-t7z", "-mx=9", "-y", dst_7z, src],
            check=True,
            capture_output=True,
            timeout=3600,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def decompress_7z(src_7z: str, out_dir: str) -> Optional[str]:
    """Extract src_7z to out_dir. Returns path to extracted file (single file) or None."""
    try:
        subprocess.run(
            ["7z", "x", "-y", f"-o{out_dir}", src_7z],
            check=True,
            capture_output=True,
            timeout=3600,
        )
        extracted = [f for f in os.listdir(out_dir) if not f.startswith(".")]
        if len(extracted) == 1:
            return os.path.join(out_dir, extracted[0])
        for f in extracted:
            if f.lower().endswith(".m4a"):
                return os.path.join(out_dir, f)
        return os.path.join(out_dir, extracted[0]) if extracted else None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def run_zip(cfg: dict) -> None:
    """Zip mode: compress M4A under COMPRESSION_AUDIO_PATH, verify, then delete originals."""
    root = (cfg.get("COMPRESSION_AUDIO_PATH") or "").strip()
    if not root or not os.path.isdir(root):
        print("Error: COMPRESSION_AUDIO_PATH must be an existing directory.", file=sys.stderr)
        sys.exit(1)
    method = (cfg.get("COMPRESSION_METHOD") or "zstd").lower()
    zstd_level = int(cfg.get("ZSTD_LEVEL") or 19)
    state_file = cfg.get("COMPRESSION_STATE_FILE") or "compression_state.json"
    recursive = bool(cfg.get("COMPRESSION_RECURSIVE"))
    state_path = os.path.join(root, state_file)

    existing_state: dict = {"path_root": root, "mode": "zip", "updated_iso": "", "files": []}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                existing_state = json.load(f)
            existing_state["files"] = existing_state.get("files") or []
        except Exception:
            pass

    m4a_list = collect_m4a_files(root, recursive)
    if not m4a_list:
        print("No .m4a files found under", root)
        return

    print(f"Zip mode: found {len(m4a_list)} M4A file(s). Method={method}.")
    added = []
    failed = []

    for m4a_abs in tqdm(m4a_list, desc="Zip", unit="file"):
        base = os.path.splitext(m4a_abs)[0]
        rel_dir = os.path.dirname(os.path.relpath(m4a_abs, root))
        base_name = os.path.basename(base)
        if rel_dir and rel_dir != ".":
            rel_original = os.path.join(rel_dir, os.path.basename(m4a_abs))
            rel_compressed = os.path.join(rel_dir, base_name + ".m4a.zst" if method == "zstd" else base_name + ".m4a.7z")
            rel_meta = os.path.join(rel_dir, base_name + ".meta.json")
        else:
            rel_original = os.path.basename(m4a_abs)
            rel_compressed = base_name + ".m4a.zst" if method == "zstd" else base_name + ".m4a.7z"
            rel_meta = base_name + ".meta.json"

        compressed_abs = os.path.join(root, rel_compressed)
        meta_abs = os.path.join(root, rel_meta)

        if os.path.exists(compressed_abs):
            tqdm.write("  Skip (already compressed): " + rel_original)
            continue

        size_original = os.path.getsize(m4a_abs)
        min_size_mb = int(cfg.get("COMPRESSION_MIN_SIZE_MB") or 0)
        if min_size_mb > 0 and size_original < min_size_mb * 1024 * 1024:
            tqdm.write("  Skip (below min size " + str(min_size_mb) + " MB): " + rel_original)
            continue
        sha = sha256_file(m4a_abs)
        meta_ff = get_meta_ffprobe(m4a_abs)

        if method == "zstd":
            ok = compress_zstd(m4a_abs, compressed_abs, zstd_level)
        else:
            ok = compress_7z(m4a_abs, compressed_abs)
        if not ok:
            tqdm.write("  Failed to compress: " + rel_original)
            failed.append(rel_original)
            continue

        size_compressed = os.path.getsize(compressed_abs)

        # Verify: decompress and compare SHA256
        with tempfile.TemporaryDirectory() as tmp:
            if method == "zstd":
                restored = os.path.join(tmp, "restored.m4a")
                if not decompress_zstd(compressed_abs, restored):
                    tqdm.write("  Verify decompress failed: " + rel_original)
                    failed.append(rel_original)
                    try:
                        os.remove(compressed_abs)
                    except OSError:
                        pass
                    continue
            else:
                rpath = decompress_7z(compressed_abs, tmp)
                if not rpath or not os.path.isfile(rpath):
                    tqdm.write("  Verify decompress failed: " + rel_original)
                    failed.append(rel_original)
                    try:
                        os.remove(compressed_abs)
                    except OSError:
                        pass
                    continue
                restored = rpath

            sha_restored = sha256_file(restored)
            if sha_restored != sha:
                tqdm.write("  Verify SHA256 mismatch: " + rel_original)
                failed.append(rel_original)
                try:
                    os.remove(compressed_abs)
                except OSError:
                    pass
                continue

        meta_obj = {
            "sha256": sha,
            "size_original": size_original,
            "size_compressed": size_compressed,
            "mime_type": "audio/mp4",
            "sample_rate": meta_ff["sample_rate"],
            "channels": meta_ff["channels"],
            "duration_sec": meta_ff["duration_sec"],
            "codec": meta_ff["codec"],
            "original_path": rel_original,
            "compressed_path": rel_compressed,
            "created_iso": datetime.utcnow().isoformat() + "Z",
        }
        os.makedirs(os.path.dirname(meta_abs) or ".", exist_ok=True)
        with open(meta_abs, "w", encoding="utf-8") as f:
            json.dump(meta_obj, f, indent=2, ensure_ascii=False)

        try:
            os.remove(m4a_abs)
        except OSError as e:
            tqdm.write("  Failed to remove original: " + rel_original + " " + str(e))
            failed.append(rel_original)
            continue

        entry = {
            "id": base_name,
            "original_rel": rel_original,
            "compressed_rel": rel_compressed,
            "meta_rel": rel_meta,
            "sha256": sha,
            "size_original": size_original,
            "size_compressed": size_compressed,
        }
        added.append(entry)
        existing_state["files"].append(entry)
        tqdm.write("  OK: " + rel_original + " -> " + rel_compressed)

    existing_state["updated_iso"] = datetime.utcnow().isoformat() + "Z"
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(existing_state, f, indent=2, ensure_ascii=False)

    print(f"Done. Compressed {len(added)}, failed {len(failed)}. State: {state_path}")


def run_unzip(cfg: dict) -> None:
    """Unzip mode: decompress .m4a.zst (or .7z) under COMPRESSION_AUDIO_PATH, verify SHA256."""
    root = (cfg.get("COMPRESSION_AUDIO_PATH") or "").strip()
    if not root or not os.path.isdir(root):
        print("Error: COMPRESSION_AUDIO_PATH must be an existing directory.", file=sys.stderr)
        sys.exit(1)
    method = (cfg.get("COMPRESSION_METHOD") or "zstd").lower()
    state_file = cfg.get("COMPRESSION_STATE_FILE") or "compression_state.json"
    state_path = os.path.join(root, state_file)

    files_to_restore: List[Dict[str, Any]] = []
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            files_to_restore = data.get("files") or []
        except Exception as e:
            print("Warning: could not load state file:", e, file=sys.stderr)

    if not files_to_restore:
        # Fallback: scan for .m4a.zst / .m4a.7z
        for dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                if f.endswith(".m4a.zst") or (method == "7z" and f.endswith(".m4a.7z")):
                    abs_path = os.path.join(dirpath, f)
                    rel = os.path.relpath(abs_path, root)
                    meta_path = abs_path.replace(".m4a.zst", ".meta.json").replace(".m4a.7z", ".meta.json")
                    if os.path.exists(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as mf:
                                meta = json.load(mf)
                            files_to_restore.append({
                                "compressed_rel": rel,
                                "meta_rel": os.path.relpath(meta_path, root),
                                "original_rel": meta.get("original_path", rel.replace(".m4a.zst", ".m4a").replace(".m4a.7z", ".m4a")),
                                "sha256": meta.get("sha256"),
                            })
                        except Exception:
                            pass
    if not files_to_restore:
        print("No compressed entries found (state empty or no .m4a.zst/.meta.json).")
        return

    print(f"Unzip mode: restoring {len(files_to_restore)} file(s).")
    ok_count = 0
    fail_count = 0
    for entry in tqdm(files_to_restore, desc="Unzip", unit="file"):
        comp_rel = entry.get("compressed_rel") or ""
        meta_rel = entry.get("meta_rel") or ""
        orig_rel = entry.get("original_rel") or comp_rel.replace(".m4a.zst", ".m4a").replace(".m4a.7z", ".m4a")
        comp_abs = os.path.join(root, comp_rel)
        meta_abs = os.path.join(root, meta_rel)
        orig_abs = os.path.join(root, orig_rel)

        if not os.path.exists(comp_abs):
            tqdm.write("  Skip (missing): " + comp_rel)
            fail_count += 1
            continue
        if os.path.exists(orig_abs):
            tqdm.write("  Skip (already exists): " + orig_rel)
            if cfg.get("COMPRESSION_DELETE_AFTER_UNZIP"):
                for p in (comp_abs, meta_abs):
                    if p and os.path.isfile(p):
                        try:
                            os.remove(p)
                            tqdm.write("    Deleted: " + os.path.basename(p))
                        except OSError as e:
                            tqdm.write("    Warning: could not delete " + os.path.basename(p) + ": " + str(e))
            continue

        expected_sha = entry.get("sha256")
        if not expected_sha and os.path.exists(meta_abs):
            try:
                with open(meta_abs, "r", encoding="utf-8") as f:
                    expected_sha = json.load(f).get("sha256")
            except Exception:
                pass

        os.makedirs(os.path.dirname(orig_abs) or ".", exist_ok=True)
        if method == "zstd" and comp_abs.endswith(".zst"):
            if not decompress_zstd(comp_abs, orig_abs):
                tqdm.write("  Failed to decompress: " + comp_rel)
                fail_count += 1
                continue
        elif comp_abs.endswith(".7z"):
            parent = os.path.dirname(orig_abs)
            os.makedirs(parent, exist_ok=True)
            extracted = decompress_7z(comp_abs, parent)
            if not extracted or not os.path.isfile(extracted):
                tqdm.write("  Failed to decompress: " + comp_rel)
                fail_count += 1
                continue
            if os.path.abspath(extracted) != os.path.abspath(orig_abs):
                shutil.move(extracted, orig_abs)
        else:
            tqdm.write("  Skip (method/extension mismatch): " + comp_rel)
            fail_count += 1
            continue

        if expected_sha:
            actual = sha256_file(orig_abs)
            if actual != expected_sha:
                tqdm.write("  SHA256 mismatch: " + orig_rel)
                try:
                    os.remove(orig_abs)
                except OSError:
                    pass
                fail_count += 1
                continue
        ok_count += 1
        tqdm.write("  OK: " + orig_rel)

        if cfg.get("COMPRESSION_DELETE_AFTER_UNZIP"):
            for p in (comp_abs, meta_abs):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError as e:
                        tqdm.write("  Warning: could not delete " + os.path.basename(p) + ": " + str(e))

    print(f"Done. Restored {ok_count}, failed {fail_count}.")


def main() -> None:
    cfg = load_compression_config()
    path = (cfg.get("COMPRESSION_AUDIO_PATH") or "").strip()
    mode = (cfg.get("COMPRESSION_MODE") or "zip").strip().lower()

    if not path:
        print("Usage: Set COMPRESSION_AUDIO_PATH in config.py to the archive root (e.g. SSD path).", file=sys.stderr)
        print("Then set COMPRESSION_MODE to 'zip' or 'unzip' and run: python zip_process.py", file=sys.stderr)
        sys.exit(1)

    if mode == "unzip":
        run_unzip(cfg)
    else:
        run_zip(cfg)


if __name__ == "__main__":
    main()
