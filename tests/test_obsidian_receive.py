from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.obsidian_receive import ReceiveError, receive

CID_A = "UC" + "a" * 22
CID_B = "UC" + "b" * 22
VID_A = "abcdefghijk"
VID_B = "lmnopqrstuv"


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Files:
    def __init__(self, items, bodies):
        self.items, self.bodies = items, bodies

    def list(self, **kwargs):
        return _Request({"files": self.items})

    def get_media(self, *, fileId):
        return _Request(self.bodies[fileId])


class _Drive:
    def __init__(self, items, bodies):
        self._files = _Files(items, bodies)

    def files(self):
        return self._files


def _item(fid, cid, vid, body, name=None, **overrides):
    item = {
        "id": fid,
        "name": name or f"Alpha_Title_{vid}_ko_5-mini.md",
        "parents": ["1777fpf3nO2-4ISKAt_3D1ipiwgJddYY-"],
        "appProperties": {"p03_video_id": vid, "p03_kind": "summary", "p03_channel_id": cid},
        "md5Checksum": hashlib.md5(body).hexdigest(),
        "_body": body,
    }
    item.update(overrides)
    return item


def _setup(tmp_path, items):
    vault = tmp_path / "vault"
    vault.mkdir()
    channels = tmp_path / "channel_df.csv"
    with channels.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["channel_id", "channel_name", "obsidian"])
        w.writerow([CID_A, "Alpha", "recording"])
        w.writerow([CID_B, "Beta", ""])
    bodies = {item["id"]: item["_body"] for item in items}
    return vault, channels, tmp_path / "state" / "ledger.json", _Drive(items, bodies), bodies


def _receive(setup, **kwargs):
    vault, channels, ledger, drive, _ = setup
    return receive(service=drive, channel_csv=channels, vault=vault, state_path=ledger, **kwargs)


def test_source_identity_binding_and_only_selected_channels(tmp_path):
    good = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    wrong_channel = _item("drive-b", CID_B, VID_B, b"# drive-b\n")
    legacy_channel = _item("drive-legacy", CID_A, "12345678903", b"# legacy\n", appProperties={"p03_video_id": "12345678903", "p03_kind": "summary"})
    forged_parent = _item("drive-c", CID_A, "12345678901", b"# drive-c\n", parents=["other"])
    wrong_kind = _item("drive-d", CID_A, "12345678902", b"# drive-d\n", appProperties={"p03_video_id": "12345678902", "p03_kind": "metadata", "p03_channel_id": CID_A})
    setup = _setup(tmp_path, [good, legacy_channel, wrong_channel, forged_parent, wrong_kind])
    assert _receive(setup) == {"received": 2, "updated": 0, "candidates": 0, "skipped": 0, "errors": 0}
    assert (setup[0] / good["name"]).read_bytes() == setup[4]["drive-a"]
    assert (setup[0] / legacy_channel["name"]).read_bytes() == setup[4]["drive-legacy"]
    assert len(list(setup[0].iterdir())) == 2


def test_unchanged_skips_and_user_edit_becomes_update_candidate(tmp_path):
    item = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    setup = _setup(tmp_path, [item])
    assert _receive(setup)["received"] == 1
    assert _receive(setup)["skipped"] == 1
    original = setup[0] / item["name"]
    original.write_bytes(b"user edit\n")
    assert _receive(setup)["candidates"] == 1
    assert original.read_bytes() == b"user edit\n"
    candidate = setup[0] / item["name"].replace(".md", f".update-candidate-{item['md5Checksum'][:8]}.md")
    assert candidate.read_bytes() == setup[4]["drive-a"]


def test_existing_candidate_names_are_preserved_and_next_name_is_used(tmp_path):
    item = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    setup = _setup(tmp_path, [item])
    assert _receive(setup)["received"] == 1
    original = setup[0] / item["name"]
    original.write_bytes(b"user edit\n")
    first = setup[0] / item["name"].replace(".md", f".update-candidate-{item['md5Checksum'][:8]}.md")
    second = first.with_name(first.stem + "-2.md")
    first.write_bytes(b"first user file\n")
    second.write_bytes(b"second user file\n")
    assert _receive(setup)["candidates"] == 1
    assert first.read_bytes() == b"first user file\n"
    assert second.read_bytes() == b"second user file\n"
    assert (setup[0] / (first.stem + "-3.md")).read_bytes() == setup[4]["drive-a"]


def test_candidate_install_race_uses_next_name_without_replacing_file(tmp_path, monkeypatch):
    import os
    from scripts import obsidian_receive as receiver

    item = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    setup = _setup(tmp_path, [item])
    assert _receive(setup)["received"] == 1
    (setup[0] / item["name"]).write_bytes(b"user edit\n")
    candidate = setup[0] / item["name"].replace(".md", f".update-candidate-{item['md5Checksum'][:8]}.md")
    real_link = os.link
    raced = False

    def link_with_race(src, dst, *args, **kwargs):
        nonlocal raced
        if not raced and dst == candidate.name:
            candidate.write_bytes(b"arrived during install\n")
            raced = True
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(receiver.os, "link", link_with_race)
    assert _receive(setup)["candidates"] == 1
    assert candidate.read_bytes() == b"arrived during install\n"
    assert (setup[0] / candidate.with_name(candidate.stem + "-2.md").name).read_bytes() == setup[4]["drive-a"]


def test_drive_revision_updates_owned_note_but_never_deletes(tmp_path):
    first = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    setup = _setup(tmp_path, [first])
    assert _receive(setup)["received"] == 1
    note = setup[0] / first["name"]
    first_body = note.read_bytes()
    second_body = b"# revised\n"
    setup[4]["drive-a"] = second_body
    first["md5Checksum"] = hashlib.md5(second_body).hexdigest()
    assert _receive(setup)["updated"] == 1
    assert note.read_bytes() == second_body and note.read_bytes() != first_body
    # A removed Drive item is not processed as a deletion, and local notes remain.
    setup[3]._files = _Files([], {})
    assert _receive(setup)["received"] == 0
    assert note.is_file()


def test_bounded_count_and_token_never_appears_in_output(tmp_path, monkeypatch, capsys):
    items = [_item(f"drive-{n}", CID_A, f"{n:011d}", f"# {n}\n".encode()) for n in (1, 2, 3)]
    setup = _setup(tmp_path, items)
    assert _receive(setup, max_count=2)["received"] == 2
    monkeypatch.setenv("P03_DRIVE_TOKEN_FILE", "/secret/token-path")
    from scripts import obsidian_receive as receiver
    monkeypatch.setattr(receiver, "receive", lambda: (_ for _ in ()).throw(ReceiveError("safe refusal")))
    assert receiver.main() == 2
    captured = capsys.readouterr()
    assert "safe refusal" in captured.err
    assert "/secret/token-path" not in captured.out + captured.err


def test_bound_progresses_past_unchanged_items(tmp_path):
    first = _item("drive-a", CID_A, VID_A, b"# unchanged\n")
    setup = _setup(tmp_path, [first])
    assert _receive(setup)["received"] == 1
    second = _item("drive-b", CID_A, VID_B, b"# new note\n")
    setup[3]._files = _Files([first, second], {"drive-a": first["_body"], "drive-b": second["_body"]})
    result = _receive(setup, max_count=1)
    assert result == {"received": 1, "updated": 0, "candidates": 0, "skipped": 1, "errors": 0}
    assert (setup[0] / second["name"]).read_bytes() == second["_body"]


def test_launcher_module_command_runs_in_subprocess_with_fake_environment(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    launcher = (repo / "launchd/run-p03-obsidian-receive.sh.example").read_text()
    assert "python3 -m scripts.obsidian_receive" in launcher
    channels = tmp_path / "channels.csv"
    channels.write_text("channel_id,obsidian\n", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "P03_CHANNEL_CSV": str(channels), "OUTPUT_MD_PATH": str(vault),
           "P03_STATE_DIR": str(tmp_path / "state")}
    result = subprocess.run([sys.executable, "-m", "scripts.obsidian_receive"], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2
    assert "P03_DRIVE_TOKEN_FILE must point" in result.stderr
    assert "No module named scripts" not in result.stderr


def test_path_symlink_is_refused_and_does_not_touch_target(tmp_path):
    item = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    setup = _setup(tmp_path, [item])
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"keep")
    (setup[0] / item["name"]).symlink_to(outside)
    with pytest.raises(ReceiveError):
        _receive(setup)
    assert outside.read_bytes() == b"keep"


def test_destination_symlink_substitution_during_atomic_replace_is_safe(tmp_path, monkeypatch):
    import os
    from scripts import obsidian_receive as receiver

    item = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    setup = _setup(tmp_path, [item])
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"keep")
    real_replace = os.replace
    swapped = False

    def replace_with_swap(src, dst, *args, **kwargs):
        nonlocal swapped
        if not swapped and dst == item["name"] and "dst_dir_fd" in kwargs:
            (setup[0] / item["name"]).symlink_to(outside)
            swapped = True
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(receiver.os, "replace", replace_with_swap)
    assert _receive(setup)["received"] == 1
    assert swapped
    assert outside.read_bytes() == b"keep"
    assert (setup[0] / item["name"]).read_bytes() == setup[4]["drive-a"]


@pytest.mark.parametrize("bad_csv", [b"", b"channel_id,obsidian\nnot-a-channel,recording\n", b"channel_id,obsidian\nvalid," + b"recording,extra\n"])
def test_missing_malformed_or_partial_selection_fails_closed(tmp_path, bad_csv):
    vault = tmp_path / "vault"
    vault.mkdir()
    channels = tmp_path / "channel_df.csv"
    channels.write_bytes(bad_csv)
    with pytest.raises(ReceiveError):
        receive(service=_Drive([], {}), channel_csv=channels, vault=vault, state_path=tmp_path / "state.json")


def test_oversized_input_and_malformed_nested_ledger_fail_closed(tmp_path):
    item = _item("drive-a", CID_A, VID_A, b"# drive-a\n")
    setup = _setup(tmp_path, [item])
    setup[2].parent.mkdir()
    setup[2].write_text(json.dumps({"version": 1, "files": {"drive-a": {"md5": "bad"}}}))
    with pytest.raises(ReceiveError):
        _receive(setup)
    setup[2].unlink()
    setup[1].write_bytes(b"channel_id,channel_name,obsidian\n" + b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ReceiveError):
        _receive(setup)
