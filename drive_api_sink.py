"""Verified Google Drive API sink for P03 per-video artifacts."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from scripts.drive_yt_summary.manifest import (
    inventory_completion_decision,
    merge_manifest_source,
    parse_manifest_yaml,
    restore_missing_manifest_fields,
    serialize_manifest_yaml,
    set_manifest_inventory,
    verify_inventory_evidence,
)

SOURCE_FOLDER_ID = "1777fpf3nO2-4ISKAt_3D1ipiwgJddYY-"
SUMMARY_FOLDER_ID = "1S-sGDHvkSC1tTKWPYWiSgxmGN2Fz7Clu"
MANIFEST_FILE_ID = "1NUIcUNlEyYwjHVngghwNlmh4VKiIk7oW"
INVENTORY_FILE_NAME = "manifest.inventory.json"
TOKEN_ENV = "P03_DRIVE_TOKEN_FILE"
MANIFEST_FILE_ID_ENV = "P03_MANIFEST_FILE_ID"
INVENTORY_FILE_ID_ENV = "P03_INVENTORY_FILE_ID"
_MANIFEST_THREAD_LOCKS = {}
_MANIFEST_THREAD_LOCKS_GUARD = threading.Lock()


class DriveUploadError(RuntimeError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_item_tokens(item):
    return {
        (key, str(item.get(key) or "").strip())
        for key in ("drive_file_id", "video_id", "file")
        if str(item.get(key) or "").strip()
    }


def _manifest_retains_items(before_doc, after_doc):
    after_tokens = [_manifest_item_tokens(item) for item in after_doc.get("items", [])]
    return all(
        any(_manifest_item_tokens(item) & tokens for tokens in after_tokens)
        for item in before_doc.get("items", [])
    )


class DriveApiSink:
    """Uploads files idempotently and verifies Drive metadata before returning."""

    def __init__(self, service=None, *, source_folder_id=SOURCE_FOLDER_ID, summary_folder_id=SUMMARY_FOLDER_ID):
        self._drive = service or self._build_service()
        self.source_folder_id = source_folder_id
        self.summary_folder_id = summary_folder_id
        self._transcripts_id = None

    @property
    def manifest_file_id(self):
        return (os.getenv(MANIFEST_FILE_ID_ENV) or "").strip() or MANIFEST_FILE_ID

    @contextmanager
    def _manifest_lock(self):
        """Serialize all local processes that write the same canonical manifest."""
        import fcntl

        import hashlib

        lock_hash = hashlib.sha256(self.manifest_file_id.encode("utf-8")).hexdigest()[:20]
        lock_path = (os.getenv("P03_MANIFEST_LOCK_PATH") or
                     os.path.join(tempfile.gettempdir(), f"p03-manifest-{lock_hash}.lock"))
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        with _MANIFEST_THREAD_LOCKS_GUARD:
            thread_lock = _MANIFEST_THREAD_LOCKS.setdefault(lock_path, threading.RLock())
        with thread_lock:
            with open(lock_path, "a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _manifest_metadata(self):
        try:
            meta = self._drive.files().get(
                fileId=self.manifest_file_id,
                fields="id,name,mimeType,parents,headRevisionId,md5Checksum",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            raise DriveUploadError(f"canonical manifest lookup failed: {type(exc).__name__}") from exc
        if (meta.get("id") != self.manifest_file_id or
                meta.get("name") != "manifest.yaml" or
                self.summary_folder_id not in meta.get("parents", [])):
            raise DriveUploadError("canonical manifest identity or parent verification failed")
        if not re.fullmatch(r"[0-9a-f]{32}", str(meta.get("md5Checksum", ""))):
            raise DriveUploadError("canonical manifest metadata has no valid MD5 checksum")
        return meta

    def _download_manifest(self, meta=None):
        meta = meta or self._manifest_metadata()
        try:
            payload = self._drive.files().get_media(fileId=self.manifest_file_id).execute()
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            payload = bytes(payload)
        except Exception as exc:
            raise DriveUploadError(f"canonical manifest download failed: {type(exc).__name__}") from exc
        if hashlib.md5(payload).hexdigest() != meta.get("md5Checksum"):
            raise DriveUploadError("canonical manifest download checksum does not match Drive metadata")
        try:
            doc = parse_manifest_yaml(payload)
        except Exception as exc:
            raise DriveUploadError(f"canonical manifest YAML is invalid: {type(exc).__name__}") from exc
        return payload, doc

    def read_manifest(self):
        """Read the fixed canonical manifest and validate its Drive checksum."""
        _, doc = self._download_manifest()
        return doc

    @staticmethod
    def _manifest_signature(meta):
        return (str(meta.get("headRevisionId") or ""), str(meta.get("md5Checksum") or ""))

    def _modify_manifest(self, mutator, *, max_attempts=5, after_verified=None):
        """Locked read/merge/update with a last-moment revision/checksum check."""
        from googleapiclient.http import MediaIoBaseUpload

        with self._manifest_lock():
            last_conflict = False
            preserved_docs = []
            for attempt in range(max_attempts):
                meta = self._manifest_metadata()
                try:
                    current_payload, doc = self._download_manifest(meta)
                except DriveUploadError as exc:
                    if "checksum does not match Drive metadata" in str(exc):
                        last_conflict = True
                        continue
                    raise
                try:
                    doc, restored_missing = restore_missing_manifest_fields(doc, preserved_docs)
                except ValueError as exc:
                    raise DriveUploadError(str(exc)) from exc
                try:
                    updated_doc, result, changed = mutator(doc)
                except DriveUploadError:
                    raise
                except Exception as exc:
                    raise DriveUploadError(f"canonical manifest merge failed: {type(exc).__name__}: {exc}") from exc
                if not changed and not restored_missing:
                    if after_verified:
                        return after_verified(current_payload, doc, result)
                    return result
                payload = serialize_manifest_yaml(updated_doc)

                # Drive has no atomic compare-and-swap here. Recheck its current
                # head/checksum immediately before update; conflicts restart the
                # merge from the newly observed document.
                latest_meta = self._manifest_metadata()
                if self._manifest_signature(meta) != self._manifest_signature(latest_meta):
                    last_conflict = True
                    continue
                try:
                    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/yaml", resumable=False)
                    self._drive.files().update(
                        fileId=self.manifest_file_id,
                        body={"name": "manifest.yaml"},
                        media_body=media,
                        fields="id,headRevisionId,md5Checksum",
                        supportsAllDrives=True,
                    ).execute()
                    readback_meta = self._manifest_metadata()
                    readback = self._drive.files().get_media(fileId=self.manifest_file_id).execute()
                    if isinstance(readback, str):
                        readback = readback.encode("utf-8")
                    readback = bytes(readback)
                    readback_md5_valid = hashlib.md5(readback).hexdigest() == readback_meta.get("md5Checksum")
                    if not readback_md5_valid:
                        raise DriveUploadError("canonical manifest SHA-256 readback verification failed")
                    try:
                        readback_doc = parse_manifest_yaml(readback)
                    except Exception as exc:
                        raise DriveUploadError("canonical manifest readback YAML is invalid") from exc
                    expected_revision = str(latest_meta.get("headRevisionId") or "")
                    observed_revision = str(readback_meta.get("headRevisionId") or "")
                    revision_jump = bool(
                        expected_revision.isdigit() and observed_revision.isdigit() and
                        int(observed_revision) != int(expected_revision) + 1
                    )
                    if (revision_jump or not _manifest_retains_items(doc, readback_doc) or
                            _sha(readback) != _sha(payload)):
                        # A valid but unexpected readback indicates an external
                        # writer raced the unconditional Drive update. Preserve all
                        # observed rows and re-merge from the new head, bounded.
                        preserved_docs.extend((doc, readback_doc))
                        last_conflict = True
                        continue
                    if after_verified:
                        return after_verified(readback, updated_doc, result)
                    return result
                except DriveUploadError:
                    raise
                except Exception as exc:
                    raise DriveUploadError(f"canonical manifest update/readback failed: {type(exc).__name__}") from exc
            reason = "changed during every update attempt" if last_conflict else "update attempts exhausted"
            raise DriveUploadError(f"canonical manifest lost-update guard failed: {reason}")

    @staticmethod
    def _summary_title(summary: bytes, summary_name: str) -> str:
        fallback = Path(summary_name).stem
        try:
            from scripts.note_catalog_utils import strip_leading_frontmatter
            from scripts.md_mobile_utils import extract_title

            content = summary.decode("utf-8-sig")
            return extract_title(strip_leading_frontmatter(content), fallback=fallback) or fallback
        except Exception:
            return fallback

    def _merge_manifest_summary(self, *, summary_name, summary, video_id, drive_file_id, recovered=False):
        digest = _sha(summary)
        title = self._summary_title(summary, summary_name)

        def merge(doc):
            return merge_manifest_source(
                doc,
                file=summary_name,
                title=title,
                content_hash=digest,
                video_id=video_id,
                drive_file_id=drive_file_id,
                recovered=recovered,
            )

        change = self._modify_manifest(merge)
        return {**change, "drive_file_id": drive_file_id}

    def write_inventory(self, inventory: dict, *, start=False, success_video_ids=()):
        """Write provisional inventory to the manifest and verified completion beside it.

        The manifest never advertises completion. A separate JSON evidence file is
        published only after exact manifest readback, and the caller receives a
        complete record only after that evidence file also passes readback.
        """
        incoming = dict(inventory)
        success_ids = {str(value).strip() for value in success_video_ids if str(value).strip()}
        source_counts = incoming.get("source_counts") or {}
        expected_success_count = int(source_counts.get("manifest_succeeded", source_counts.get("succeeded", 0)))

        def merge(doc):
            current_inventory = doc.get("inventory") or {}
            if not start and current_inventory.get("producer_run_id") != incoming.get("producer_run_id"):
                raise DriveUploadError("inventory run was superseded before completion")
            present = {
                str(item.get("video_id") or "")
                for item in doc.get("items", [])
                if item.get("file") and re.fullmatch(r"[0-9a-f]{64}", str(item.get("hash") or ""))
            }
            all_successes_present = (len(success_ids) == expected_success_count and success_ids.issubset(present))
            record = dict(incoming)
            record["all_successes_present"] = all_successes_present
            # A manifest cannot prove that its own bytes were read back. Keep its
            # completion flags false in every state; completion lives in the
            # separately checksummed evidence file below.
            record["manifest_readback_verified"] = False
            record["inventory_complete"] = False
            updated, stored_record = set_manifest_inventory(doc, record, start=start)
            return updated, stored_record, updated.get("inventory") != doc.get("inventory")

        def publish_verified_evidence(manifest_payload, _doc, stored_record):
            all_successes_present = bool(
                stored_record.get("all_successes_present") and
                len(success_ids) == expected_success_count
            )
            ready = inventory_completion_decision(
                stored_record,
                manifest_readback_verified=True,
                inventory_file_readback_verified=False,
                all_successes_present=all_successes_present,
            )
            if not ready["ready_for_inventory_file_verification"]:
                return stored_record
            staged_record = dict(stored_record)
            staged_record.update({
                "manifest_readback_verified": True,
                "inventory_file_readback_verified": False,
                "inventory_complete": False,
            })
            staged_evidence = {
                "schema_version": 1,
                "manifest_file_id": self.manifest_file_id,
                "manifest_sha256": _sha(manifest_payload),
                "inventory": staged_record,
            }
            # First persist and read back a provisional artifact. Only that
            # positive Drive readback lets the central gate authorize true.
            self._upsert_inventory_evidence(staged_evidence)
            decision = inventory_completion_decision(
                staged_record,
                manifest_readback_verified=True,
                inventory_file_readback_verified=True,
                all_successes_present=all_successes_present,
            )
            if not decision["inventory_complete"]:
                return stored_record
            completed_record = dict(staged_record)
            completed_record["inventory_file_readback_verified"] = True
            completed_record["inventory_complete"] = decision["inventory_complete"]
            evidence = {**staged_evidence, "inventory": completed_record}
            self._upsert_inventory_evidence(evidence)
            # This value is a return receipt, not a self-assertion stored in the
            # artifact. Consumers independently verify the downloaded MD5/SHA and
            # that manifest_sha256 names the current canonical bytes.
            return {**completed_record, "inventory_file_readback_verified": True}

        try:
            return self._modify_manifest(merge, after_verified=publish_verified_evidence)
        except DriveUploadError:
            # Best-effort clearing of the provisional state. If this corrective
            # write fails, the separate evidence still fails its manifest hash
            # match whenever the attempted manifest update changed the bytes.
            if not start:
                failed_record = dict(incoming)
                failed_record.update({
                    "manifest_readback_verified": False,
                    "all_successes_present": False,
                    "inventory_complete": False,
                })
                try:
                    def mark_incomplete(doc):
                        updated, stored_record = set_manifest_inventory(doc, failed_record, start=False)
                        return updated, stored_record, updated.get("inventory") != doc.get("inventory")
                    self._modify_manifest(mark_incomplete)
                except Exception:
                    pass
            raise

    def _inventory_metadata(self):
        configured_id = (os.getenv(INVENTORY_FILE_ID_ENV) or "").strip()
        try:
            files = self._drive.files()
            matches = []
            page_token = None
            while True:
                kwargs = {
                    "q": f"'{self.summary_folder_id}' in parents and name='{INVENTORY_FILE_NAME}' and trashed=false",
                    "spaces": "drive", "pageSize": 1000,
                    "fields": "nextPageToken,files(id,name,mimeType,parents,md5Checksum,headRevisionId)",
                    "supportsAllDrives": True, "includeItemsFromAllDrives": True,
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                response = files.list(**kwargs).execute()
                matches.extend(
                    item for item in response.get("files", [])
                    if item.get("name") == INVENTORY_FILE_NAME and
                    self.summary_folder_id in item.get("parents", [])
                )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            raise DriveUploadError(f"inventory evidence search failed: {type(exc).__name__}") from exc
        if len(matches) > 1:
            raise DriveUploadError("multiple manifest inventory evidence files exist; refusing ambiguous completion")
        if configured_id and (not matches or matches[0].get("id") != configured_id):
            raise DriveUploadError("configured inventory evidence file ID does not match the unique sibling file")
        if not matches:
            return None
        meta = matches[0]
        if (meta.get("name") != INVENTORY_FILE_NAME or
                meta.get("mimeType") != "application/json" or
                self.summary_folder_id not in meta.get("parents", [])):
            raise DriveUploadError("inventory evidence identity or parent verification failed")
        return meta

    def _upsert_inventory_evidence(self, evidence: dict):
        """Create the sibling evidence once, then update its fixed Drive ID in place."""
        from googleapiclient.http import MediaIoBaseUpload

        payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        meta = self._inventory_metadata()
        file_id = meta.get("id") if meta else ""
        if (os.getenv(INVENTORY_FILE_ID_ENV) or "").strip() and not meta:
            raise DriveUploadError("configured inventory evidence file ID is not present under the canonical parent")
        if meta and (os.getenv(INVENTORY_FILE_ID_ENV) or "").strip() and file_id != os.getenv(INVENTORY_FILE_ID_ENV).strip():
            raise DriveUploadError("configured inventory evidence file ID does not match Drive metadata")
        try:
            media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False)
            if file_id:
                self._drive.files().update(
                    fileId=file_id,
                    body={"name": INVENTORY_FILE_NAME},
                    media_body=media,
                    fields="id,md5Checksum,headRevisionId",
                    supportsAllDrives=True,
                ).execute()
            else:
                file_id = self._drive.files().create(
                    body={
                        "name": INVENTORY_FILE_NAME,
                        "mimeType": "application/json",
                        "parents": [self.summary_folder_id],
                        "appProperties": {"p03_kind": "manifest_inventory", "manifest_file_id": self.manifest_file_id},
                    },
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()["id"]
            read_meta = self._drive.files().get(
                fileId=file_id,
                fields="id,name,mimeType,parents,md5Checksum,headRevisionId",
                supportsAllDrives=True,
            ).execute()
            readback = self._drive.files().get_media(fileId=file_id).execute()
            if isinstance(readback, str):
                readback = readback.encode("utf-8")
            readback = bytes(readback)
            if (read_meta.get("id") != file_id or read_meta.get("name") != INVENTORY_FILE_NAME or
                    read_meta.get("mimeType") != "application/json" or
                    self.summary_folder_id not in read_meta.get("parents", []) or
                    hashlib.md5(readback).hexdigest() != read_meta.get("md5Checksum") or
                    _sha(readback) != _sha(payload) or json.loads(readback.decode("utf-8")) != evidence):
                raise DriveUploadError("inventory evidence SHA-256 readback verification failed")
            # Catch duplicate creation by a non-cooperating first-time writer.
            current = self._inventory_metadata()
            if not current or current.get("id") != file_id:
                raise DriveUploadError("inventory evidence file identity changed after write")
            return file_id
        except DriveUploadError:
            raise
        except Exception as exc:
            raise DriveUploadError(f"inventory evidence update/readback failed: {type(exc).__name__}") from exc

    def read_inventory_evidence(self):
        """Read and verify the sibling evidence against the current manifest bytes."""
        manifest_meta = self._manifest_metadata()
        manifest_payload = self._drive.files().get_media(fileId=self.manifest_file_id).execute()
        if isinstance(manifest_payload, str):
            manifest_payload = manifest_payload.encode("utf-8")
        manifest_payload = bytes(manifest_payload)
        if hashlib.md5(manifest_payload).hexdigest() != manifest_meta.get("md5Checksum"):
            raise DriveUploadError("canonical manifest download checksum does not match Drive metadata")
        evidence_meta = self._inventory_metadata()
        if not evidence_meta:
            return None
        evidence_payload = self._drive.files().get_media(fileId=evidence_meta["id"]).execute()
        if isinstance(evidence_payload, str):
            evidence_payload = evidence_payload.encode("utf-8")
        try:
            return verify_inventory_evidence(
                manifest_payload, bytes(evidence_payload),
                evidence_md5=evidence_meta.get("md5Checksum", ""),
                expected_manifest_file_id=self.manifest_file_id,
            )
        except Exception as exc:
            raise DriveUploadError(f"inventory evidence verification failed: {exc}") from exc

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
                meta = files.get(fileId=fid, fields="id,name,parents,size,md5Checksum,appProperties,headRevisionId", supportsAllDrives=True).execute()
                expected_md5 = hashlib.md5(data).hexdigest()
                if (fid != meta.get("id") or name != meta.get("name") or parent not in meta.get("parents", []) or
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

    def _state_backup_folder(self):
        """Find or create the dedicated state-backup folder under YT_summary."""
        files = self._drive.files()
        resp = files.list(
            q=f"'{self.summary_folder_id}' in parents and name='state-backup' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=100, fields="files(id,name,mimeType,parents)", supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        matches = [f for f in resp.get("files", []) if f.get("name") == "state-backup" and
                   f.get("mimeType") == "application/vnd.google-apps.folder" and
                   self.summary_folder_id in f.get("parents", [])]
        if matches:
            return matches[0]["id"]
        return files.create(body={"name": "state-backup", "mimeType": "application/vnd.google-apps.folder",
                                 "parents": [self.summary_folder_id]}, fields="id", supportsAllDrives=True).execute()["id"]

    def _upload_state_backup(self, parent: str, name: str, data: bytes):
        from googleapiclient.http import MediaIoBaseUpload
        if not re.fullmatch(r"p03-state-\d{8}T\d{6}Z\.tar\.gz", name) or len(data) > 64 * 1024 * 1024:
            raise DriveUploadError("invalid state archive name or size")
        files = self._drive.files()
        body = {"name": name, "appProperties": {"p03_kind": "state_backup"}}
        result = files.create(body={**body, "parents": [parent]},
            media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype="application/gzip", resumable=False),
            fields="id", supportsAllDrives=True).execute()
        fid = result["id"]
        meta = files.get(fileId=fid, fields="id,name,parents,size,md5Checksum,appProperties", supportsAllDrives=True).execute()
        failures = []
        if meta.get("id") != fid: failures.append("id")
        if meta.get("name") != name: failures.append("name")
        if parent not in meta.get("parents", []): failures.append("parent")
        if int(meta.get("size", -1)) != len(data): failures.append("size")
        if meta.get("md5Checksum") != hashlib.md5(data).hexdigest(): failures.append("md5")
        if meta.get("appProperties", {}).get("p03_kind") != "state_backup": failures.append("kind")
        if failures:
            raise DriveUploadError("Drive readback verification failed for state backup: " + ",".join(failures))
        return meta

    def _prune_state_backups(self, parent: str, *, keep: int, newest_id: str):
        files = self._drive.files()
        listed = []
        page_token = None
        while True:
            kwargs = {"q": f"'{parent}' in parents and trashed=false", "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,parents,appProperties,createdTime)", "supportsAllDrives": True,
                "includeItemsFromAllDrives": True}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = files.list(**kwargs).execute()
            listed.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        owned = [f for f in listed if f.get("appProperties", {}).get("p03_kind") == "state_backup"
                 and parent in f.get("parents", []) and re.fullmatch(r"p03-state-\d{8}T\d{6}Z\.tar\.gz", str(f.get("name", "")))]
        owned.sort(key=lambda f: (f.get("name", ""), f.get("createdTime", "")), reverse=True)
        # Include a just-uploaded file even when the API's list is briefly stale.
        if not any(f.get("id") == newest_id for f in owned):
            return
        for old in owned[keep:]:
            if old.get("id") != newest_id and old.get("appProperties", {}).get("p03_kind") == "state_backup":
                files.delete(fileId=old["id"], supportsAllDrives=True).execute()

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
        manifest_change = self._merge_manifest_summary(
            summary_name=summary_name, summary=summary, video_id=video_id,
            drive_file_id=summary_meta["id"], recovered=False,
        )
        return {"summary": summary_meta, "transcript": transcript_meta, "metadata": meta_meta,
                "transcripts_folder_id": transcripts_id, "manifest": manifest_change}

    def already_uploaded(self, *, video_id: str, transcript: bytes) -> bool:
        """True only when all three Drive artifacts match this cleaned-caption revision."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")) or not isinstance(transcript, bytes):
            raise DriveUploadError("invalid video identity or caption revision input")
        input_revision = _sha(transcript)
        try:
            summaries = self._drive.files().list(q=f"'{self.source_folder_id}' in parents and trashed=false",
                fields="files(id,name,appProperties)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
            transcript_folder = self._transcripts_folder()
            children = self._drive.files().list(q=f"'{transcript_folder}' in parents and trashed=false",
                fields="files(id,name,appProperties)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
        except Exception as exc:
            raise DriveUploadError(f"Drive dedupe lookup failed: {type(exc).__name__}") from exc
        all_files = summaries + children
        matched = {}
        for item in all_files:
            props = item.get("appProperties", {})
            kind = props.get("p03_kind")
            if (kind in {"summary", "transcript", "metadata"} and
                    props.get("p03_video_id") == video_id and
                    props.get("p03_input_revision") == input_revision):
                matched.setdefault(kind, item)
        if set(matched) != {"summary", "transcript", "metadata"}:
            return False
        expected_transcript_md5 = hashlib.md5(transcript).hexdigest()
        try:
            readbacks = {kind: self._drive.files().get(
                fileId=item["id"], fields="id,name,parents,size,md5Checksum,appProperties",
                supportsAllDrives=True).execute() for kind, item in matched.items()}
            for kind, meta in readbacks.items():
                parent = self.source_folder_id if kind == "summary" else transcript_folder
                props = meta.get("appProperties", {})
                if (meta.get("id") != matched[kind].get("id") or
                        parent not in meta.get("parents", []) or
                        meta.get("name") != matched[kind].get("name") or
                        int(meta.get("size", -1)) < 0 or
                        not re.fullmatch(r"[0-9a-f]{32}", str(meta.get("md5Checksum", ""))) or
                        props.get("p03_video_id") != video_id or
                        props.get("p03_kind") != kind or
                        props.get("p03_input_revision") != input_revision):
                    return False
            transcript_meta = readbacks["transcript"]
            if (int(transcript_meta["size"]) != len(transcript) or
                    transcript_meta["md5Checksum"] != expected_transcript_md5):
                return False
            # Metadata records the summary and transcript digests, which bind the
            # summary bytes to this exact input revision.
            metadata_content = self._drive.files().get_media(fileId=matched["metadata"]["id"]).execute()
            if isinstance(metadata_content, str):
                metadata_content = metadata_content.encode("utf-8")
            metadata_meta = readbacks["metadata"]
            if (len(metadata_content) != int(metadata_meta["size"]) or
                    hashlib.md5(metadata_content).hexdigest() != metadata_meta["md5Checksum"]):
                return False
            metadata = json.loads(metadata_content.decode("utf-8"))
            summary_meta = readbacks["summary"]
            if (int(summary_meta["size"]) <= 0 or
                    metadata.get("video_id") != video_id or
                    metadata.get("transcript_sha256") != input_revision or
                    not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("summary_sha256", ""))) or
                    int(summary_meta["size"]) <= 0):
                return False
            # Drive's md5 is an independently read back checksum; metadata stores
            # SHA-256. Fetch the summary bytes to compare both representations.
            summary_content = self._drive.files().get_media(fileId=matched["summary"]["id"]).execute()
            if isinstance(summary_content, str):
                summary_content = summary_content.encode("utf-8")
            verified = (_sha(summary_content) == metadata["summary_sha256"] and
                        hashlib.md5(summary_content).hexdigest() == summary_meta["md5Checksum"] and
                        len(summary_content) == int(summary_meta["size"]))
        except Exception:
            return False
        if not verified:
            return False
        try:
            self._merge_manifest_summary(
                summary_name=str(summary_meta.get("name") or ""),
                summary=summary_content,
                video_id=video_id,
                drive_file_id=str(summary_meta.get("id") or ""),
                recovered=True,
            )
        except Exception as exc:
            if isinstance(exc, DriveUploadError):
                raise
            raise DriveUploadError(f"canonical manifest repair failed: {type(exc).__name__}") from exc
        return True
