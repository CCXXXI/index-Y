from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "sync"))
sys.path.insert(0, str(ROOT / "scripts"))

pr = importlib.import_module("prepare_review")
tt = importlib.import_module("triage_text")
uc = importlib.import_module("upstream_context")


def review_block(**overrides) -> dict:
    block = {
        "id": 0,
        "files": ["【S1_01】甲、乙/OEBPS/Text/S1_01-01_a.xhtml"],
        "old": "旧甲\n- 旧乙\n★ 旧丙",
        "new": "新甲\n+ 新乙\n★ 新丙",
        "commits": [],
        "annotations": [],
    }
    block.update(overrides)
    return block


def test_parse_blocks_uses_json_as_machine_authority(tmp_path: Path) -> None:
    triage = tmp_path / ".triage"
    triage.mkdir()
    expected = review_block()
    (triage / "review_changes.json").write_text(
        json.dumps([expected], ensure_ascii=False), encoding="utf-8"
    )
    (triage / "review_changes.txt").write_text(
        "[1次] wrong.xhtml\n- 有损旧文本\n+ 有损新文本\n\n", encoding="utf-8"
    )

    assert pr.parse_blocks(str(triage)) == [expected]


def test_parse_blocks_rejects_legacy_txt_without_silent_loss(tmp_path: Path) -> None:
    triage = tmp_path / ".triage"
    triage.mkdir()
    (triage / "review_changes.txt").write_text(
        "[1次] chapter.xhtml\n- 旧\n+ 新\n\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="迁移.*triage_text"):
        pr.parse_blocks(str(triage))


def test_triage_export_writes_fixed_json_and_human_txt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    triage = tmp_path / ".triage"
    expected = review_block()
    rel = expected["files"][0]
    monkeypatch.setattr(tt, "STATE_DIR", str(triage))
    monkeypatch.setattr(tt, "prune_hold", list)
    monkeypatch.setattr(tt, "read_hold", dict)
    monkeypatch.setattr(tt, "clean_scratch", lambda: None)
    monkeypatch.setattr(
        tt,
        "collect",
        lambda: {
            "blocks": {rel: [(expected["old"], expected["new"])]},
            "new_files": [],
            "deleted": [],
            "nontext": [],
            "format_only": [],
            "rule_fx": [],
        },
    )
    monkeypatch.setattr(sys, "argv", ["triage_text.py"])

    tt.main()

    json_path = triage / "review_changes.json"
    assert json_path.exists(), "JSON 必须是跨脚本机器权威产物"
    assert json.loads(json_path.read_text(encoding="utf-8")) == [expected]
    txt = (triage / "review_changes.txt").read_text(encoding="utf-8")
    assert rel in txt
    assert expected["old"] in txt
    assert expected["new"] in txt


def test_upstream_annotations_update_json_and_regenerate_txt(tmp_path: Path) -> None:
    triage = tmp_path / ".triage"
    old = "旧原文句子甲乙丙丁"
    new = "新原文句子戊己庚辛"
    tt.write_review_changes(str(triage), [review_block(old=old, new=new)])
    corpus = {"docs/maintenance-records/record.md": f"记录：{old} → {new}"}
    commits = [uc.CommitDiff("deadbeef", "修正译文", old, new)]

    assert uc.annotate(str(triage / "review_changes.txt"), corpus, commits) == (1, 1)

    annotated = json.loads(
        (triage / "review_changes.json").read_text(encoding="utf-8")
    )[0]
    assert annotated["commits"] == ["deadbeef"]
    assert annotated["annotations"][0] == "commit deadbeef: 修正译文"
    assert any(a.startswith("record.md: ") for a in annotated["annotations"])
    assert all(not a.startswith("★") for a in annotated["annotations"])
    txt = (triage / "review_changes.txt").read_text(encoding="utf-8")
    assert "★ commit deadbeef: 修正译文\n" in txt
    assert "★ record.md: " in txt


def test_xhtml_timestamp_text_is_not_filtered() -> None:
    old = b"<html><body><p>2026-01-01T01:02:03</p></body></html>"
    new = b"<html><body><p>2027-02-02T01:02:03</p></body></html>"

    assert tt.block_changes(old, new) == [
        ("2026-01-01T01:02:03", "2027-02-02T01:02:03")
    ]


def test_opf_filter_is_limited_to_dcterms_modified_timestamp_node() -> None:
    old = b"""<package><metadata>
<meta property="dcterms:modified">2026-01-01T01:02:03Z</meta>
<meta name="other">2026-01-01T01:02:03Z</meta>
</metadata></package>"""
    new = b"""<package><metadata>
<meta property="dcterms:modified">2027-02-02T01:02:03Z</meta>
<meta name="other">2027-02-02T01:02:03Z</meta>
</metadata></package>"""

    assert tt.block_changes(old, new, "content.opf") == [
        ("2026-01-01T01:02:03Z", "2027-02-02T01:02:03Z")
    ]


def test_write_task_collects_all_block_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    triage = tmp_path / ".triage"
    for name in ("review_chunks", "review_prompts", "verdicts"):
        (triage / name).mkdir(parents=True)
    files = [
        "【S1_01】一卷/OEBPS/Text/S1_01-01_a.xhtml",
        "【S1_02】二卷/OEBPS/Text/S1_02-01_a.xhtml",
    ]
    monkeypatch.setattr(pr, "locate_block", lambda *args: None)

    pr.write_task(
        str(tmp_path),
        str(triage),
        0,
        [review_block(files=files)],
        {},
        {},
        {},
        {"S1_01": "first.txt", "S1_02": "second.txt"},
        [],
    )

    task = json.loads(
        (triage / "review_chunks" / "task_00.json").read_text(encoding="utf-8")
    )
    prompt = (triage / "review_prompts" / "task_00.md").read_text(encoding="utf-8")
    assert task["vols"] == ["S1_01", "S1_02"]
    assert "first.txt" in prompt
    assert "second.txt" in prompt


def test_prepare_marks_cross_volume_exemption_and_uses_all_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    triage = root / ".triage"
    files = [
        "【S1_01】一卷/OEBPS/Text/S1_01-01_a.xhtml",
        "【S1_02】二卷/OEBPS/Text/S1_02-01_a.xhtml",
    ]
    tt.write_review_changes(str(triage), [review_block(files=files)])
    for rel in files:
        (root / "index-X" / "EPUB" / rel.split("/")[0]).mkdir(parents=True)
    jp_root = tmp_path / "index-jp"
    jp_root.mkdir()
    (jp_root / "no_original.txt").write_text("S1_01\nS1_02\n", encoding="utf-8")
    monkeypatch.setattr(pr, "repo_root", lambda: str(root))
    monkeypatch.setattr(sys, "argv", ["prepare_review.py"])

    assert pr.main() == 0

    blocks = json.loads((triage / "review_blocks.json").read_text(encoding="utf-8"))
    assert [b.get("jp_exempt") for b in blocks] == [True]
    prompt = (triage / "review_prompts" / "task_00.md").read_text(encoding="utf-8")
    assert "豁免原文对照：以下卷" in prompt
    assert "S1_01、S1_02" in prompt


def test_missing_original_removes_stale_text_and_aligned_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    vol_dir = tmp_path / "index-jp" / "【S1_01】一卷"
    page = vol_dir / "item" / "xhtml" / "p001.xhtml"
    page.parent.mkdir(parents=True)
    page.write_text("<html><body><p>旧原文</p></body></html>", encoding="utf-8")
    aligned_dir = tmp_path / "aligned" / "S1_01"
    aligned_dir.mkdir(parents=True)
    (aligned_dir / "S1_01-01.xhtml").write_text("<p>旧对齐原文</p>", encoding="utf-8")
    paths, missing = pr.build_jp_text(str(root), ["S1_01"])
    aligned_paths = pr.build_aligned_text(
        str(root), ["S1_01"], {"S1_01": str(aligned_dir)}
    )
    assert missing == []
    assert Path(paths["S1_01"]).exists()
    assert Path(aligned_paths["S1_01"]).exists()

    shutil.rmtree(vol_dir)
    paths2, missing2 = pr.build_jp_text(str(root), ["S1_01"])

    assert paths2 == {}
    assert missing2 == ["S1_01"]
    assert not Path(paths["S1_01"]).exists()
    assert not Path(aligned_paths["S1_01"]).exists()


def test_unaligned_volume_removes_stale_aligned_artifact(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    stale = root / ".triage" / "jp_text" / "S1_01.aligned.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("陈旧对齐原文", encoding="utf-8")

    assert pr.build_aligned_text(str(root), ["S1_01"], {}) == {}
    assert not stale.exists()


def test_prepare_does_not_recreate_aligned_text_for_missing_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    triage = root / ".triage"
    rel = "【S1_01】一卷/OEBPS/Text/S1_01-01_a.xhtml"
    tt.write_review_changes(str(triage), [review_block(files=[rel])])
    (root / "index-X" / "EPUB" / "【S1_01】一卷").mkdir(parents=True)
    jp_root = tmp_path / "index-jp"
    jp_root.mkdir()
    (jp_root / "no_original.txt").write_text("S1_01\n", encoding="utf-8")
    aligned_dir = tmp_path / "aligned" / "S1_01"
    aligned_dir.mkdir(parents=True)
    (aligned_dir / "S1_01-01.xhtml").write_text("<p>缓存原文</p>", encoding="utf-8")
    monkeypatch.setattr(pr, "repo_root", lambda: str(root))
    monkeypatch.setattr(pr, "aligned_vols", lambda root: {"S1_01": str(aligned_dir)})
    located_with = []

    def fake_locate(root, xdirs, aligned, block):
        located_with.append(aligned)

    monkeypatch.setattr(pr, "locate_block", fake_locate)
    monkeypatch.setattr(sys, "argv", ["prepare_review.py"])

    assert pr.main() == 0
    assert not (triage / "jp_text" / "S1_01.aligned.txt").exists()
    assert located_with == [{}]


def test_prepare_pending_strictly_validates_old_metadata_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    triage = root / ".triage"
    current = [review_block(id=i, old=f"旧{i}", new=f"新{i}") for i in range(3)]
    tt.write_review_changes(str(triage), current)
    old_blocks = [dict(b, jp_exempt=True) for b in current]
    (triage / "review_blocks.json").write_text(
        json.dumps(old_blocks, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for name in ("review_chunks", "review_prompts", "verdicts"):
        (triage / name).mkdir(parents=True, exist_ok=True)
    (triage / "review_chunks/task_00.json").write_text(
        json.dumps({"blocks": old_blocks[:1]}), encoding="utf-8"
    )
    (triage / "review_prompts/task_00.md").write_text("旧任务", encoding="utf-8")
    records = [
        {"id": 0, "verdict": "ok", "cls": "semantic", "jp": "", "reason": "旧豁免"},
        {
            "id": 1,
            "verdict": "suspect",
            "cls": "semantic",
            "jp": "编造原文",
            "reason": "非法证据",
        },
    ]
    (triage / "verdicts/task_00.jsonl").write_text(
        "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in records),
        encoding="utf-8",
    )
    jp_text = triage / "jp_text/S1_01.txt"
    jp_text.parent.mkdir(parents=True)
    jp_text.write_text("真实原文", encoding="utf-8")
    (root / "index-X/EPUB/【S1_01】甲、乙").mkdir(parents=True)
    page = tmp_path / "index-jp/【S1_01】甲、乙/item/xhtml/p001.xhtml"
    page.parent.mkdir(parents=True)
    page.write_text("<p>真实原文</p>", encoding="utf-8")
    before = {
        str(path.relative_to(triage)): path.read_bytes()
        for path in triage.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(pr, "repo_root", lambda: str(root))
    monkeypatch.setattr(sys, "argv", ["prepare_review.py", "--pending"])

    rc = pr.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "id 1" in out
    assert "id 0" not in out
    after = {
        str(path.relative_to(triage)): path.read_bytes()
        for path in triage.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_empty_input_writes_empty_blocks_and_clears_old_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    triage = root / ".triage"
    tt.write_review_changes(str(triage), [])
    (triage / "review_blocks.json").write_text(
        json.dumps([review_block()], ensure_ascii=False), encoding="utf-8"
    )
    stale_files = {
        "review_chunks": "task_00.json",
        "review_prompts": "task_00.md",
        "verdicts": "task_00.jsonl",
    }
    for directory, name in stale_files.items():
        path = triage / directory / name
        path.parent.mkdir(parents=True)
        path.write_text("stale" if name != "task_00.jsonl" else "", encoding="utf-8")
    (triage / "sample_plan.json").write_text("stale-plan", encoding="utf-8")
    (triage / "sample_plan.json.tmp").write_text("stale-tmp", encoding="utf-8")
    monkeypatch.setattr(pr, "repo_root", lambda: str(root))
    monkeypatch.setattr(sys, "argv", ["prepare_review.py"])

    assert pr.main() == 0

    assert json.loads((triage / "review_blocks.json").read_text(encoding="utf-8")) == []
    for directory, name in stale_files.items():
        assert not (triage / directory / name).exists()
    assert not (triage / "sample_plan.json").exists()
    assert not (triage / "sample_plan.json.tmp").exists()


@pytest.mark.parametrize("range_kind", ["reverse", "diverged"])
def test_upstream_rejects_non_forward_ranges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    range_kind: str,
) -> None:
    base, head = "b" * 40, "a" * 40
    merge_base = head if range_kind == "reverse" else "c" * 40
    monkeypatch.setattr(uc, "STATE_DIR", str(tmp_path / ".triage"))
    monkeypatch.setattr(uc, "pin_sha", lambda: base)
    monkeypatch.setattr(uc, "x_head", lambda: head)
    monkeypatch.setattr(uc, "changed_vols", list)
    monkeypatch.setattr(uc, "records_index", lambda head: [])
    monkeypatch.setattr(uc, "changed_records", lambda base, head: [])
    monkeypatch.setattr(uc, "policy_changes", lambda base, head: [])

    def fake_gitx(*args: str, input_bytes: bytes | None = None) -> bytes:
        if args[0] == "merge-base":
            return f"{merge_base}\n".encode()
        if args[0] == "rev-parse":
            return f"{head}\n".encode()
        if args[0] == "rev-list":
            return b"0\n"
        if args[0] in ("tag", "log"):
            return b""
        raise AssertionError(f"unexpected gitx call: {args}")

    monkeypatch.setattr(uc, "gitx", fake_gitx)
    monkeypatch.setattr(sys, "argv", ["upstream_context.py"])

    assert uc.main() != 0
    assert "反向/分叉" in capsys.readouterr().out
    assert not (tmp_path / ".triage" / "upstream_context.txt").exists()


def test_review_prompts_use_portable_repository_paths() -> None:
    for prompt in (pr.PROMPT, pr.PROMPT_ALIGNED):
        assert "C:\\Users\\" not in prompt
        assert "ccxxx" not in prompt
        assert "index-Y 仓库根目录" in prompt
    assert "由运行环境提供实际路径" in pr.PROMPT
