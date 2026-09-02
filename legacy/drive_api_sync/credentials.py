#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve Google Drive credentials for YT_summary sync (retired API path)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
AUTH_MODE_ENV = "GOOGLE_DRIVE_AUTH_MODE"
DEFAULT_AUTH_MODE = "service_account"


@dataclass(frozen=True)
class DriveCredentialResolution:
    credentials: Any
    source: str  # service_account | oauth_user
    auth_mode: str
    token_path: Optional[str] = None
    service_account_file: Optional[str] = None
    service_account_email: Optional[str] = None
    oauth_failure: Optional[str] = None


def _auth_mode() -> str:
    raw = (os.getenv(AUTH_MODE_ENV) or "").strip().lower()
    return raw or DEFAULT_AUTH_MODE


def _oauth_token_path() -> Path:
    raw = (os.getenv("GOOGLE_DRIVE_OAUTH_TOKEN_FILE") or "").strip()
    default = Path.home() / "Developer/GCP/google-docs-connection/pi_drive_user_token.json"
    return Path(raw).expanduser() if raw else default


def _load_service_account(service_account_file: Path) -> DriveCredentialResolution:
    if not service_account_file.is_file():
        raise RuntimeError(
            f"Service account file not found: {service_account_file} "
            "(set GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE)"
        )
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        str(service_account_file),
        scopes=[DRIVE_SCOPE],
    )
    return DriveCredentialResolution(
        credentials=creds,
        source="service_account",
        auth_mode="service_account",
        service_account_file=str(service_account_file),
        service_account_email=creds.service_account_email or "",
    )


def _load_oauth(token_path: Path) -> DriveCredentialResolution:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.is_file():
        raise RuntimeError(f"OAuth token file not found: {token_path}")
    creds = Credentials.from_authorized_user_file(str(token_path), [DRIVE_SCOPE])
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        raise RuntimeError(f"OAuth token invalid: {token_path}")
    return DriveCredentialResolution(
        credentials=creds,
        source="oauth_user",
        auth_mode="oauth",
        token_path=str(token_path),
    )


def resolve_drive_credentials(service_account_file: Path) -> DriveCredentialResolution:
    """Resolve Drive credentials per GOOGLE_DRIVE_AUTH_MODE (default: service_account)."""
    mode = _auth_mode()
    if mode == "service_account":
        return _load_service_account(service_account_file)
    if mode in {"oauth", "oauth_user", "installed_app"}:
        return _load_oauth(_oauth_token_path())
    raise RuntimeError(
        f"Unsupported {AUTH_MODE_ENV}={mode!r} "
        f"(expected service_account or oauth)"
    )
