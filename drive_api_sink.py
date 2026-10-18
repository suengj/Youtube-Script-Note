"""Verified Google Drive API sink for P03 per-video artifacts."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCE_FOLDER_ID = "1777fpf3nO2-4ISKAt_3D1ipiwgJddYY-"
SUMMARY_FOLDER_ID = "1S-sGDHvkSC1tTKWPYWiSgxmGN2Fz7Clu"
TOKEN_ENV = "P03_DRIVE_TOKEN_FILE"


class DriveUploadError(RuntimeError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DriveApiSink:
    """Uploads files idempotently and verifies Drive metadata before returning."""

    def __init__(self, service=None, *, source_folder_id=SOURCE_FOLDER_ID, summary_folder_id=SUMMARY_FOLDER_ID):
        self._drive = service or self._build_service()
        self.source_folder_id = source_folder_id
        self.summary_folder_id = summary_folder_id
        self._transcripts_id = None

    @staticmethod
    def _build_service():
        token_path = (os.getenv(TOKEN_ENV) or "").strip()
        if not token_path:
            raise DriveUploadError(f"{TOKEN_ENV} must point to the authorized_user token file")
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(str(Path(token_path).expanduser()), ["https://www.googleapis.com/auth/drive"])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())  # in-memory refresh; never persist token changes
        if not creds.valid:
            raise DriveUploadError("Drive OAuth credentials are invalid")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _find(self, parent: str, name: str, video_id: str, kind: str, input_revision: str = ""):
        escaped = name.replace("'", "\\'")
        q = f"'{parent}' in parents and name='{escaped}' and trashed=false"
        resp = self._drive.files().list(q=q, spaces="drive", pageSize=100,
            fields="files(id,name,parents,appProperties)", supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        matches = [f for f in resp.get("files", []) if f.get("appProperties", {}).get("p03_video_id") == video_id and f.get("appProperties", {}).get("p03_kind") == kind and (not input_revision or f.get("appProperties", {}).get("p03_input_revision") == input_revision)]
        if not matches:
            # Dedupe by canonical video identity even when a title change altered the filename.
            all_resp = self._drive.files().list(q=f"'{parent}' in parents and trashed=false", spaces="drive", pageSize=1000,
                fields="files(id,name,parents,appProperties)", supportsAllDrives=True,
                includeItemsFromAllDrives=True).execute()
            matches = [f for f in all_resp.get("files", []) if f.get("appProperties", {}).get("p03_video_id") == video_id and f.get("appProperties", {}).get("p03_kind") == kind]
        if not matches:
            # Migration bridge for same-name files created before appProperties existed.
            matches = [f for f in resp.get("files", []) if f.get("name") == name and video_id in name]
        return matches[0] if matches else None

    def _upload(self, parent: str, name: str, data: bytes, video_id: str, kind: str, mime: str, input_revision: str):
        from googleapiclient.http import MediaIoBaseUpload
        body = {"name": name, "appProperties": {"p03_video_id": video_id, "p03_kind": kind, "p03_input_revision": input_revision}}
        files = self._drive.files()
        last_error = None
        for attempt in range(3):
            try:
                # Re-query each attempt so an uncertain create response becomes an update.
                existing = self._find(parent, name, video_id, kind)
                media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
                if existing:
                    fid = existing["id"]
                    files.update(fileId=fid, body=body, media_body=media, fields="id", supportsAllDrives=True).execute()
                else:
                    create_body = {**body, "parents": [parent]}
                    fid = files.create(body=create_body, media_body=media, fields="id", supportsAllDrives=True).execute()["id"]
                meta = files.get(fileId=fid, fields="id,parents,size,md5Checksum,appProperties", supportsAllDrives=True).execute()
                expected_md5 = hashlib.md5(data).hexdigest()
                if (fid != meta.get("id") or parent not in meta.get("parents", []) or
                        int(meta.get("size", -1)) != len(data) or meta.get("md5Checksum") != expected_md5 or
                        meta.get("appProperties", {}).get("p03_video_id") != video_id or
                        meta.get("appProperties", {}).get("p03_kind") != kind or
                        meta.get("appProperties", {}).get("p03_input_revision") != input_revision):
                    raise DriveUploadError(f"Drive readback verification failed for {kind}")
                return meta
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        if isinstance(last_error, DriveUploadError):
            raise last_error
        raise DriveUploadError(f"Drive upload/readback failed for {kind}: {type(last_error).__name__}") from last_error

    def _transcripts_folder(self):
        if self._transcripts_id:
            return self._transcripts_id
        files = self._drive.files()
        resp = files.list(q=f"'{self.summary_folder_id}' in parents and name='transcripts' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        folders = resp.get("files", [])
        if folders:
            self._transcripts_id = folders[0]["id"]
        else:
            result = files.create(body={"name":"transcripts", "mimeType":"application/vnd.google-apps.folder", "parents":[self.summary_folder_id]}, fields="id", supportsAllDrives=True).execute()
            self._transcripts_id = result["id"]
        return self._transcripts_id

    def upload_video(self, *, summary_name: str, summary: bytes, transcript: bytes, metadata: dict, video_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")):
            raise DriveUploadError("invalid or missing YouTube video id")
        if (not isinstance(summary_name, str) or not summary_name.endswith(".md") or
                summary_name in {".", ".."} or "/" in summary_name or "\\" in summary_name or
                video_id not in summary_name or len(summary_name.encode("utf-8")) > 240):
            raise DriveUploadError("invalid summary filename or filename/video identity mismatch")
        if not isinstance(summary, bytes) or not isinstance(transcript, bytes):
            raise DriveUploadError("summary and transcript must be bytes")
        if not isinstance(metadata, dict):
            raise DriveUploadError("metadata must be a mapping")
        if len(summary) > 128 * 1024 * 1024 or len(transcript) > 128 * 1024 * 1024:
            raise DriveUploadError("summary or transcript exceeds the 128 MiB upload limit")
        stem = summary_name[:-3] if summary_name.endswith(".md") else summary_name
        metadata = dict(metadata)
        input_revision = _sha(transcript)
        metadata.update({"video_id": video_id, "transcript_sha256": input_revision, "summary_sha256": _sha(summary), "uploaded_at": datetime.now(timezone.utc).isoformat()})
        summary_meta = self._upload(self.source_folder_id, summary_name, summary, video_id, "summary", "text/markdown", input_revision)
        transcripts_id = self._transcripts_folder()
        transcript_name = stem + ".transcript.md"
        transcript_meta = self._upload(transcripts_id, transcript_name, transcript, video_id, "transcript", "text/markdown", input_revision)
        meta_data = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        meta_meta = self._upload(transcripts_id, stem + ".meta.json", meta_data, video_id, "metadata", "application/json", input_revision)
        return {"summary": summary_meta, "transcript": transcript_meta, "metadata": meta_meta, "transcripts_folder_id": transcripts_id}

    def already_uploaded(self, *, video_id: str, transcript: bytes) -> bool:
        """True only when all three Drive artifacts match this cleaned-caption revision."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")) or not isinstance(transcript, bytes):
            raise DriveUploadError("invalid video identity or caption revision input")
        input_revision = _sha(transcript)
        try:
            summaries = self._drive.files().list(q=f"'{self.source_folder_id}' in parents and trashed=false",
                fields="files(id,appProperties)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
            transcript_folder = self._transcripts_folder()
            children = self._drive.files().list(q=f"'{transcript_folder}' in parents and trashed=false",
                fields="files(id,appProperties)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
        except Exception as exc:
            raise DriveUploadError(f"Drive dedupe lookup failed: {type(exc).__name__}") from exc
        all_files = summaries + children
        kinds = {f.get("appProperties", {}).get("p03_kind") for f in all_files
                 if f.get("appProperties", {}).get("p03_video_id") == video_id
                 and f.get("appProperties", {}).get("p03_input_revision") == input_revision}
        return {"summary", "transcript", "metadata"}.issubset(kinds)
