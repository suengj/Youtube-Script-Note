"""Regression coverage for permissions on atomically replaced data CSVs."""
import os
import shutil
import stat
import subprocess
import tempfile

import pandas as pd
import pytest

from atomic_io import preserve_replace_permissions
from manual_video_input import LEDGER_COLUMNS, save_ledger


def _rewrite_ledger(path):
    save_ledger(pd.DataFrame(columns=LEDGER_COLUMNS), path)


def test_ledger_rewrite_preserves_mode(tmp_path):
    path = tmp_path / "manual_input_status.csv"
    path.write_text("old data")
    path.chmod(0o640)
    _rewrite_ledger(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_ledger_rewrite_preserves_access_acl(tmp_path):
    setfacl, getfacl = shutil.which("setfacl"), shutil.which("getfacl")
    if not setfacl or not getfacl:
        pytest.skip("setfacl and getfacl are required for the ACL regression")
    path = tmp_path / "manual_input_status.csv"
    path.write_text("old data")
    result = subprocess.run([setfacl, "-m", f"u:{os.getuid()}:r--,m::r--", str(path)], capture_output=True, text=True)
    if result.returncode:
        pytest.skip(f"temporary filesystem does not support setting ACLs: {result.stderr.strip()}")
    _rewrite_ledger(path)
    result = subprocess.run([getfacl, "-n", "-c", str(path)], capture_output=True, text=True, check=True)
    entries = result.stdout.splitlines()
    assert f"user:{os.getuid()}:r--" in entries
    assert "mask::r--" in entries


def test_new_file_uses_umask_default(tmp_path):
    path = tmp_path / "new.csv"
    old_mask = os.umask(0o022)
    try:
        _rewrite_ledger(path)
    finally:
        os.umask(old_mask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_permission_failure_does_not_prevent_replace(tmp_path, monkeypatch, caplog):
    destination = tmp_path / "existing.csv"
    destination.write_text("old")
    fd, temp = tempfile.mkstemp(dir=tmp_path)
    os.close(fd)
    with open(temp, "w") as stream:
        stream.write("new")
    def fail_chmod(*args):
        raise PermissionError("test failure")
    monkeypatch.setattr(os, "chmod", fail_chmod)
    preserve_replace_permissions(temp, destination)
    os.replace(temp, destination)
    assert destination.read_text() == "new"
    assert "Could not set permissions" in caplog.text
