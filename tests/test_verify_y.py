from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def load_apply_module():
    spec = importlib.util.spec_from_file_location(
        "test_verify_y_apply_rules", REPO / "scripts/agent_proofread/apply_rules.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_y_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, y_text: str):
    root = tmp_path / "repo"
    vol = "【S1_01】fixture"
    rel = Path("OEBPS/Text/S1_01-01.xhtml")
    x = root / "index-X/EPUB" / vol / rel
    x.parent.mkdir(parents=True)
    x.write_text("<p>甲丙</p>", encoding="utf-8")
    y = root / "EPUB" / vol / rel
    y.parent.mkdir(parents=True)
    y.write_text(y_text, encoding="utf-8")
    rows = ["甲\t乙\t", "丙\t\t"]
    rules = root / "rules"
    rules.mkdir()
    (rules / "_common.tsv").write_text(
        "旧\t新\t注释\n不存在\t存在\t\n", encoding="utf-8"
    )
    (rules / f"{vol}.tsv").write_text(
        "\n".join(["旧\t新\t注释", *rows]) + "\n", encoding="utf-8"
    )
    candidates = root / "candidates.tsv"
    candidates.write_text("\n".join(["旧\t新\t注释", *rows]) + "\n", encoding="utf-8")
    sync_dir = root / "scripts/sync"
    sync_dir.mkdir(parents=True)
    for name in ("x2y.py", "lib_triage.py"):
        shutil.copyfile(REPO / "scripts/sync" / name, sync_dir / name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    monkeypatch.chdir(root)

    module = load_apply_module()
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "RULES_DIR", rules)
    monkeypatch.setattr(
        module, "__file__", str(root / "scripts/agent_proofread/apply_rules.py")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["apply_rules.py", "S1_01", str(candidates), "--verify-y"],
    )
    return module


def test_verify_y_rejects_stale_output_even_if_new_text_exists_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = verify_y_fixture(tmp_path, monkeypatch, "<p>甲丙乙</p>")
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code != 0


def test_verify_y_accepts_output_derived_from_current_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = verify_y_fixture(tmp_path, monkeypatch, "<p>乙</p>")
    assert module.main() == 0


@pytest.mark.parametrize(
    ("active_rows", "y_text", "missing_old"),
    [
        (["甲\t丁\t", "丙\t\t"], "<p>丁</p>", "甲"),
        (["甲\t乙\t"], "<p>乙丙</p>", "丙"),
    ],
    ids=["non-deletion-final-form", "deletion-original-hit"],
)
def test_verify_y_rejects_candidate_not_in_active_volume_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_rows: list[str],
    y_text: str,
    missing_old: str,
):
    module = verify_y_fixture(tmp_path, monkeypatch, y_text)
    target = module.RULES_DIR / "【S1_01】fixture.tsv"
    target.write_text(
        "\n".join(["旧\t新\t注释", *active_rows]) + "\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code != 0
    assert "活动分卷规则" in str(exc_info.value)
    assert missing_old in str(exc_info.value)
