"""Rename plan for vault notes cut at 255 NFD bytes (real 2026-10-07 names)."""

from __future__ import annotations

import json
import os
import sys
import unicodedata as ud
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.drive_yt_summary.state import SyncState, SyncStateEntry, save_state  # noqa: E402
from scripts.truncated_note_rename_plan import (  # noqa: E402
    DRIVE_DUP,
    RENAME,
    UNRESOLVED,
    apply_plan,
    blen,
    build_plan,
    file_hash,
    main,
    plan_to_tsv,
)

DAY = "2026_10_07"
JYY = "교양의 시대_작은 섬나라 영국은 어떻게 불패의 함대를 만들어냈을까_JyyAGZ1-r94_ko-orig_auto_subs_luna-low.md"
BE5B = (
    "실밸개발자_코드 리뷰가 병목이 되는 이유 _ 메타 시니어 엔지니어가 "
    "AI 코드 리뷰 하는법_Be5b6D1IoLI_ko-orig_auto_subs_luna-low.md"
)
CASES = [
    (JYY, "JyyAGZ1-r94", "_JyyAGZ1-r94_ko-orig_auto_subs_luna-low.md"),
    (BE5B, "Be5b6D1IoLI", "_Be5b6D1IoLI_ko-orig_auto_subs_luna-low.md"),
]
BODY = "---\nformat_version: 4.1\ntitle: T\n---\n\nBody\n"


def _cut(name: str, limit: int = 255) -> str:
    stem = ud.normalize("NFD", name)[:-3]
    out = ""
    for ch in stem:
        if len((out + ch + ".md").encode("utf-8")) > limit:
            break
        out += ch
    return out + ".md"


class Env:
    def __init__(self, tmp: Path) -> None:
        self.vault = tmp / "vault"
        (self.vault / DAY).mkdir(parents=True)
        self.drive = tmp / "YT_summary"
        (self.drive / "source").mkdir(parents=True)
        self.state_path = tmp / "state.json"
        self.files: dict = {}

    def canonical(self, name: str, body: str = BODY, vid: str = "", day: str = DAY) -> None:
        dest = self.drive / "source" / name
        dest.write_text(body, encoding="utf-8")
        rel = f"{day}/{name}"
        self.files[rel] = SyncStateEntry(
            relative_path=rel,
            content_hash=file_hash(dest),
            dest_path=str(dest),
            drive_name=name,
            updated_at="2026-10-07T00:00:00+00:00",
            video_id=vid,
        )
        save_state(self.state_path, SyncState(files=self.files))

    def vault_note(self, name: str, body: str = BODY, day: str = DAY) -> Path:
        p = self.vault / day / name
        p.write_text(body, encoding="utf-8")
        return p

    def plan(self) -> dict:
        return build_plan(self.vault, self.drive, self.state_path)


def _by_action(plan: dict, action: str) -> list:
    return [r for r in plan["rows"] if r["action"] == action]


@pytest.mark.parametrize("name,vid,tail", CASES)
def test_real_examples_are_renamed(tmp_path: Path, name: str, vid: str, tail: str) -> None:
    env = Env(tmp_path)
    env.canonical(name, vid=vid)
    cut = _cut(name)
    assert 253 <= len(ud.normalize("NFD", cut).encode("utf-8")) <= 255
    env.vault_note(cut)
    plan = env.plan()
    assert plan["counts"] == {RENAME: 1}
    row = plan["rows"][0]
    new = row["new_vault_rel"].split("/")[1]
    assert row["vault_rel"] == f"{DAY}/{cut}"
    assert new.endswith(tail)
    assert new.startswith(name.split("_", 1)[0] + "_")
    assert blen(new) <= 240
    assert new != name
    # Dry run touched nothing.
    assert (env.vault / DAY / cut).is_file() and not (env.vault / DAY / new).exists()


def test_video_id_is_derived_without_state_video_id(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY)  # no video_id recorded
    env.vault_note(_cut(JYY))
    assert env.plan()["counts"] == {RENAME: 1}


def test_ambiguous_canonicals_are_unresolved(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    env.canonical(JYY.replace("luna-low", "luna-high"), vid="JyyAGZ1-r94")
    env.vault_note(_cut(JYY))
    plan = env.plan()
    assert plan["counts"] == {UNRESOLVED: 1}
    assert plan["rows"][0]["reason"].startswith("ambiguous_canonical")


def test_content_difference_and_missing_canonical_are_unresolved(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    env.vault_note(_cut(JYY), body=BODY + "edited\n")
    env.vault_note(_cut(BE5B))  # no canonical for it
    reasons = sorted(r["reason"] for r in env.plan()["rows"])
    assert reasons == ["content_differs", "no_canonical_match"]


def test_canonical_in_other_date_folder_does_not_match(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94", day="2026_10_06")
    env.vault_note(_cut(JYY))
    assert env.plan()["rows"][0]["reason"] == "no_canonical_match"


def test_plan_marks_existing_target_unresolved(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    env.vault_note(_cut(JYY))
    new = env.plan()["rows"][0]["new_vault_rel"]
    env.vault_note(new.split("/")[1], body="other")
    rows = [r for r in env.plan()["rows"] if r["vault_rel"].endswith(_cut(JYY))]
    assert rows[0]["action"] == UNRESOLVED and rows[0]["reason"] == "target_exists"


def test_drive_duplicate_candidate(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    (env.drive / "source" / _cut(JYY)).write_text(BODY, encoding="utf-8")
    (env.drive / "source" / _cut(BE5B)).write_text(BODY, encoding="utf-8")  # unrelated: no row
    plan = env.plan()
    assert plan["counts"] == {DRIVE_DUP: 1}
    row = plan["rows"][0]
    assert row["drive_path"].endswith(_cut(JYY)) and row["canonical"] == f"{DAY}/{JYY}"
    assert (env.drive / "source" / _cut(JYY)).exists()  # never deleted


def test_drive_duplicate_with_different_content_is_unresolved(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    (env.drive / "source" / _cut(JYY)).write_text("different", encoding="utf-8")
    plan = env.plan()
    assert plan["counts"] == {UNRESOLVED: 1}


def _saved_plan(env: Env, tmp: Path) -> Path:
    p = tmp / "plan.json"
    p.write_text(json.dumps(env.plan(), ensure_ascii=False), encoding="utf-8")
    return p


def test_apply_renames_and_writes_undo_log(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    env.canonical(BE5B, vid="Be5b6D1IoLI")
    cuts = [_cut(JYY), _cut(BE5B)]
    for c in cuts:
        env.vault_note(c)
    other = env.vault_note("unrelated_short.md")
    (env.drive / "source" / cuts[0]).write_text(BODY, encoding="utf-8")  # drive dup row
    plan_path = _saved_plan(env, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["counts"] == {RENAME: 2, DRIVE_DUP: 1}

    res = apply_plan(plan_path, None, None)
    assert res["renamed"] == 2 and res["refused"] == 0
    for c in cuts:
        assert not (env.vault / DAY / c).exists()
    assert other.is_file()
    assert (env.drive / "source" / cuts[0]).is_file()  # Drive untouched, nothing deleted
    undo = json.loads(Path(res["undo_log"]).read_text(encoding="utf-8"))
    assert len(undo["renames"]) == 2
    for item in undo["renames"]:  # undo log is sufficient to reverse
        os.rename(env.vault / item["from"], env.vault / item["to"])
    assert all((env.vault / DAY / c).is_file() for c in cuts)


def test_apply_refuses_existing_target_and_changed_hash(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    env.canonical(BE5B, vid="Be5b6D1IoLI")
    cj, cb = env.vault_note(_cut(JYY)), env.vault_note(_cut(BE5B))
    plan_path = _saved_plan(env, tmp_path)
    rows = {r["vault_rel"].split("/")[1]: r for r in json.loads(plan_path.read_text(encoding="utf-8"))["rows"]}
    target = env.vault / rows[_cut(JYY)]["new_vault_rel"]
    target.write_text("precious", encoding="utf-8")  # appears after planning
    cb.write_text(BODY + "edited after planning\n", encoding="utf-8")

    res = apply_plan(plan_path, None, None)
    assert res["renamed"] == 0 and res["refused"] == 2
    assert {x["reason"] for x in res["refusals"]} == {"target_exists", "hash_changed"}
    assert cj.is_file() and cb.is_file()
    assert target.read_text(encoding="utf-8") == "precious"
    assert json.loads(Path(res["undo_log"]).read_text(encoding="utf-8"))["renames"] == []


def test_apply_ignores_non_rename_rows(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    c = env.vault_note(_cut(JYY), body="changed")
    plan_path = _saved_plan(env, tmp_path)
    res = apply_plan(plan_path, None, None)
    assert res["renamed"] == 0 and res["skipped"] == 1 and c.is_file()


def test_cli_dry_run_writes_only_requested_outputs(tmp_path: Path, capsys) -> None:
    env = Env(tmp_path)
    env.canonical(JYY, vid="JyyAGZ1-r94")
    env.vault_note(_cut(JYY))
    before = sorted(p.name for p in (env.vault / DAY).iterdir())
    rc = main(["--vault", str(env.vault), "--drive-root", str(env.drive), "--state", str(env.state_path)])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("action\t") and "RENAME" in out
    assert sorted(p.name for p in (env.vault / DAY).iterdir()) == before
    assert plan_to_tsv(env.plan()) == out
