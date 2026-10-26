from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VOL = "【S1_01】一卷"
VOL2 = "【S1_02】二卷"
REL = f"{VOL}/OEBPS/Text/S1_01-01_a.xhtml"
REL2 = f"{VOL2}/OEBPS/Text/S1_02-01_a.xhtml"


def load_review_module(name: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    monkeypatch.syspath_prepend(str(REPO / "scripts" / "sync"))
    return importlib.import_module(name)


def block(block_id: int = 0, **overrides) -> dict:
    value = {
        "id": block_id,
        "files": [REL],
        "old": "旧文",
        "new": "新文",
    }
    value.update(overrides)
    return value


def verdict(block_id: int = 0, **overrides) -> dict:
    value = {
        "id": block_id,
        "verdict": "ok",
        "cls": "form",
        "jp": "",
        "reason": "判定依据",
    }
    value.update(overrides)
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def install_original(tmp_path: Path, vol: str, text: str) -> None:
    page = tmp_path / "index-jp" / vol / "item/xhtml/p001.xhtml"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"<p>{text}</p>", encoding="utf-8")


def aggregate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocks: list[dict],
    lines: list[str],
    jp_texts: dict[str, str] | None = None,
):
    av = load_review_module("aggregate_verdicts", monkeypatch)
    root = tmp_path / "repo"
    triage = root / ".triage"
    (triage / "verdicts").mkdir(parents=True)
    write_json(triage / "review_blocks.json", blocks)
    (triage / "verdicts" / "task_00.jsonl").write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )
    for vol, text in (jp_texts or {}).items():
        path = triage / "jp_text" / f"{vol}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(av, "repo_root", lambda: str(root))
    monkeypatch.setattr(sys, "argv", ["aggregate_verdicts.py"])
    return av, root


def run_aggregate(av) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = av.main()
    return rc, stdout.getvalue()


def sample_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocks: list[dict],
    records: list[dict],
    groups: list[dict] | None = None,
    flagged: dict[str, str] | None = None,
):
    sr = load_review_module("sample_review", monkeypatch)
    root = tmp_path / "repo"
    triage = root / ".triage"
    for name in ("review_chunks", "review_prompts", "verdicts"):
        (triage / name).mkdir(parents=True, exist_ok=True)
    (root / "index-X" / "EPUB" / VOL).mkdir(parents=True)
    write_json(triage / "review_blocks.json", blocks)
    write_json(
        triage / "review_changes.json",
        [
            {
                **b,
                "commits": ["deadbeef"],
                "annotations": [],
            }
            for b in blocks
        ],
    )
    (triage / "review_changes.txt").write_text(
        "".join(
            f"[1次] {'、'.join(b['files'])}\n- {b['old']}\n+ {b['new']}\n"
            "★ commit deadbeef: 夹具\n\n"
            for b in blocks
        ),
        encoding="utf-8",
    )
    if groups is None:
        groups = [
            {
                "key": ["deadbeef", "S1_01", REL],
                "total": len(blocks),
                "sampled": [b["id"] for b in blocks[:-1]],
                "deferred": [blocks[-1]["id"]],
            }
        ]
    write_json(
        triage / "sample_plan.json",
        {
            "k": {"big": 8, "mid": 4},
            "groups": groups,
            "flagged": flagged or {},
            "tasks": [0],
        },
    )
    (triage / "verdicts" / "task_00.jsonl").write_text(
        "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in records),
        encoding="utf-8",
    )
    monkeypatch.setattr(sr, "repo_root", lambda: str(root))
    monkeypatch.setattr(sr.pr, "locate_block", lambda *args: None)
    monkeypatch.setattr(sr.pr, "aligned_vols", lambda root: {})
    monkeypatch.setattr(sr.pr, "build_jp_text", lambda root, vols: ({}, []))
    monkeypatch.setattr(sr.pr, "build_aligned_text", lambda root, vols, aligned: {})
    monkeypatch.setattr(sr.pr, "exempt_vols", lambda root: set())
    return sr, root


def sample_group(sampled: list[int], deferred: list[int]) -> list[dict]:
    return [
        {
            "key": ["deadbeef", "S1_01", REL],
            "total": len(sampled) + len(deferred),
            "sampled": sampled,
            "deferred": deferred,
        }
    ]


def install_task(root: Path, task_id: int, blocks: list[dict]) -> None:
    triage = root / ".triage"
    write_json(
        triage / f"review_chunks/task_{task_id:02d}.json",
        {"vols": ["S1_01"], "files": [REL], "blocks": blocks, "clusters": []},
    )
    (triage / f"review_prompts/task_{task_id:02d}.md").write_text(
        "fixture task", encoding="utf-8"
    )


def task_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (root / ".triage/review_chunks").glob("task_*.json")
    }


def exported_paths(root: Path) -> list[Path]:
    return [
        root / ".triage" / f"verdict_{kind}.json"
        for kind in ("suspect", "unsure", "unlocated")
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        json.dumps({"verdict": "ok", "cls": "form", "jp": "", "reason": "缺 id"}),
        json.dumps(verdict(id=True)),
        json.dumps(verdict(id=1)),
        json.dumps(verdict(verdict="suspect ")),
        json.dumps(verdict(cls="other")),
        json.dumps(verdict(jp=None)),
        json.dumps(verdict(reason=3)),
        json.dumps({"id": 0, "cls": "form", "jp": "", "reason": "缺 verdict"}),
        json.dumps({"id": 0, "verdict": "ok", "jp": "", "reason": "缺 cls"}),
        json.dumps({"id": 0, "verdict": "ok", "cls": "form", "reason": "缺 jp"}),
        json.dumps({"id": 0, "verdict": "ok", "cls": "form", "jp": ""}),
    ],
    ids=[
        "bad-json",
        "non-object",
        "missing-id",
        "bool-id",
        "unknown-id",
        "bad-verdict",
        "bad-cls",
        "non-string-jp",
        "non-string-reason",
        "missing-verdict",
        "missing-cls",
        "missing-jp",
        "missing-reason",
    ],
)
def test_aggregate_rejects_invalid_verdict_before_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
):
    av, root = aggregate_root(tmp_path, monkeypatch, [block()], [raw])

    rc, _ = run_aggregate(av)

    assert rc == 1
    assert not any(path.exists() for path in exported_paths(root))


def test_aggregate_rejects_duplicate_id_before_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    line = json.dumps(verdict())
    av, root = aggregate_root(tmp_path, monkeypatch, [block()], [line, line])

    rc, _ = run_aggregate(av)

    assert rc == 1
    assert not any(path.exists() for path in exported_paths(root))


def test_aggregate_does_not_export_incomplete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    av, root = aggregate_root(
        tmp_path, monkeypatch, [block(0), block(1)], [json.dumps(verdict(0))]
    )

    rc, _ = run_aggregate(av)

    assert rc == 1
    assert not any(path.exists() for path in exported_paths(root))


def test_sample_apply_rejects_invalid_verdict_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    records = [verdict(i, verdict="suspect " if i == 0 else "ok") for i in range(4)]
    sr, root = sample_root(tmp_path, monkeypatch, blocks, records)

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 1
    assert not (root / ".triage/verdicts/auto_sample.jsonl").exists()
    assert not list((root / ".triage/review_chunks").glob("task_*.json"))


def test_sample_plan_rejects_invalid_verdict_before_task_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    sr, root = sample_root(
        tmp_path, monkeypatch, blocks, [verdict(0, verdict="suspect ")]
    )
    chunk = root / ".triage/review_chunks/task_07.json"
    prompt = root / ".triage/review_prompts/task_07.md"
    chunk.write_text("keep-chunk", encoding="utf-8")
    prompt.write_text("keep-prompt", encoding="utf-8")

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.plan()

    assert rc == 1
    assert chunk.read_text(encoding="utf-8") == "keep-chunk"
    assert prompt.read_text(encoding="utf-8") == "keep-prompt"
    assert not (root / ".triage/review_chunks/task_00.json").exists()


def test_load_star_reads_structured_commits_without_txt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sr = load_review_module("sample_review", monkeypatch)
    triage = tmp_path / "repo/.triage"
    write_json(
        triage / "review_changes.json",
        [
            {**block(0), "commits": ["cafebabe"], "annotations": []},
            {**block(1), "commits": [], "annotations": []},
        ],
    )

    assert sr.load_star(str(triage), 2) == [["cafebabe"], []]


@pytest.mark.parametrize("kind", ["ok", "suspect", "unsure"])
@pytest.mark.parametrize("jp", ["", "  ", "…"])
def test_aggregate_requires_real_jp_for_nonexempt_semantic_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, jp: str
):
    record = verdict(cls="semantic", verdict=kind, jp=jp)
    av, root = aggregate_root(
        tmp_path,
        monkeypatch,
        [block()],
        [json.dumps(record)],
        {"S1_01": "〔p-001〕真实原文"},
    )

    rc, _ = run_aggregate(av)

    assert rc == 1
    assert not any(path.exists() for path in exported_paths(root))


def test_aggregate_rejects_ellipsis_even_for_exempt_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    record = verdict(cls="semantic", jp="…")
    av, root = aggregate_root(
        tmp_path,
        monkeypatch,
        [block(jp_exempt=True)],
        [json.dumps(record)],
        {"S1_01": "真实原文"},
    )

    rc, _ = run_aggregate(av)

    assert rc == 1
    assert not any(path.exists() for path in exported_paths(root))


def test_aggregate_allows_empty_jp_for_unlocated_exempt_and_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(0), block(1, jp_exempt=True), block(2)]
    records = [
        verdict(0, cls="semantic", verdict="unlocated"),
        verdict(1, cls="semantic"),
        verdict(2, cls="auto"),
    ]
    av, root = aggregate_root(
        tmp_path, monkeypatch, blocks, [json.dumps(v) for v in records]
    )

    rc, _ = run_aggregate(av)

    assert rc == 0
    unlocated = json.loads(
        (root / ".triage/verdict_unlocated.json").read_text(encoding="utf-8")
    )
    assert [item["id"] for item in unlocated] == [0]


def test_aggregate_verifies_jp_against_every_involved_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    second_rel = "【S1_02】二卷/OEBPS/Text/S1_02-01_a.xhtml"
    record = verdict(cls="semantic", jp="第一卷证据…第二卷固有原文")
    av, _root = aggregate_root(
        tmp_path,
        monkeypatch,
        [block(files=[REL, second_rel])],
        [json.dumps(record)],
        {"S1_01": "第一卷证据", "S1_02": "第二卷固有原文"},
    )

    rc, _ = run_aggregate(av)

    assert rc == 0


def test_aggregate_rejects_non_verbatim_jp_before_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    record = verdict(cls="semantic", verdict="suspect", jp="编造原文")
    av, root = aggregate_root(
        tmp_path,
        monkeypatch,
        [block()],
        [json.dumps(record)],
        {"S1_01": "真实原文"},
    )

    rc, _ = run_aggregate(av)

    assert rc == 1
    assert not any(path.exists() for path in exported_paths(root))


def test_sample_apply_rejects_missing_semantic_evidence_before_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    records = [verdict(0, cls="semantic")]
    records.extend(verdict(i) for i in range(1, 4))
    sr, root = sample_root(tmp_path, monkeypatch, blocks, records)

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 1
    assert not (root / ".triage/verdicts/auto_sample.jsonl").exists()
    assert not list((root / ".triage/review_chunks").glob("task_*.json"))


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("编号。", "编号3！", "alnum"),
        ("编号A。", "编号AB！", "alnum"),
        ("他来了", "他的的来了", "dup"),
        ("人人称赞。他来了。", "人人称赞。他人人来了。", "dup"),
        ("停顿", "停顿，，", "dup"),
        ("停顿", "停顿！！", "dup"),
    ],
)
def test_red_flag_catches_new_alnum_and_repetition(
    monkeypatch: pytest.MonkeyPatch, old: str, new: str, expected: str
):
    sr = load_review_module("sample_review", monkeypatch)

    assert sr.red_flag({"old": old, "new": new}) == expected


def test_red_flag_ignores_repetition_already_present_in_old(
    monkeypatch: pytest.MonkeyPatch,
):
    sr = load_review_module("sample_review", monkeypatch)

    assert sr.red_flag({"old": "他的的来了，，", "new": "他的的来了，，！"}) is None


def test_sample_plan_deep_reviews_new_number_instead_of_deferring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    blocks[2]["old"] = "编号。"
    blocks[2]["new"] = "编号3！"
    sr, root = sample_root(tmp_path, monkeypatch, blocks, [])

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.plan()

    assert rc == 0
    plan_doc = json.loads(
        (root / ".triage/sample_plan.json").read_text(encoding="utf-8")
    )
    assert plan_doc["flagged"] == {"2": "alnum"}
    assert plan_doc["groups"][0]["deferred"] == []
    task = json.loads(
        (root / ".triage/review_chunks/task_00.json").read_text(encoding="utf-8")
    )
    assert [b["id"] for b in task["blocks"]] == list(range(5))


def test_sample_apply_counts_overlapping_flagged_sample_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    records = [
        verdict(i, cls="semantic", verdict="unlocated") if i == 1 else verdict(i)
        for i in (0, 1, 3, 4)
    ]
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        records,
        groups=sample_group([0, 1, 3, 4], [2]),
        flagged={"1": "alnum"},
    )

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 0
    assert not list((root / ".triage/review_chunks").glob("task_*.json"))
    auto_path = root / ".triage/verdicts/auto_sample.jsonl"
    auto = [
        json.loads(line) for line in auto_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [v["id"] for v in auto] == [2]


def test_sample_apply_still_escalates_for_two_distinct_non_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    records = []
    for i in (0, 1, 3, 4):
        if i == 1:
            records.append(verdict(i, cls="semantic", verdict="unlocated"))
        elif i == 3:
            records.append(verdict(i, verdict="unsure"))
        else:
            records.append(verdict(i))
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        records,
        groups=sample_group([0, 1, 3, 4], [2]),
        flagged={"1": "alnum"},
    )

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 0
    assert not (root / ".triage/verdicts/auto_sample.jsonl").exists()
    task = json.loads(
        (root / ".triage/review_chunks/task_00.json").read_text(encoding="utf-8")
    )
    assert [b["id"] for b in task["blocks"]] == [2]


def test_sample_apply_repeat_does_not_duplicate_escalation_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    records = [verdict(i, verdict="suspect" if i == 0 else "ok") for i in (0, 1, 3, 4)]
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        records,
        groups=sample_group([0, 1, 3, 4], [2]),
    )
    install_task(root, 0, [blocks[i] for i in (0, 1, 3, 4)])

    with contextlib.redirect_stdout(io.StringIO()):
        assert sr.apply() == 0
    first = task_snapshot(root)
    upgrade = json.loads(
        (root / ".triage/review_chunks/task_01.json").read_text(encoding="utf-8")
    )
    assert [b["id"] for b in upgrade["blocks"]] == [2]
    persisted = json.loads(
        (root / ".triage/sample_plan.json").read_text(encoding="utf-8")
    )
    assert persisted["escalation_tasks"] == [1]

    with contextlib.redirect_stdout(io.StringIO()):
        assert sr.apply() == 0

    assert task_snapshot(root) == first


def test_sample_apply_recovers_partial_escalation_without_duplicate_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(6)]
    records = [verdict(i, verdict="suspect" if i == 0 else "ok") for i in range(4)]
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        records,
        groups=sample_group([0, 1, 2, 3], [4, 5]),
    )
    install_task(root, 0, blocks[:4])
    install_task(root, 7, [blocks[4]])
    plan_path = root / ".triage/sample_plan.json"
    plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_doc["escalation_tasks"] = [7]
    write_json(plan_path, plan_doc)

    with contextlib.redirect_stdout(io.StringIO()):
        assert sr.apply() == 0
    first = task_snapshot(root)
    persisted = json.loads(plan_path.read_text(encoding="utf-8"))
    assert persisted["escalation_tasks"] == [7, 8]
    upgrade = json.loads(
        (root / ".triage/review_chunks/task_08.json").read_text(encoding="utf-8")
    )
    assert [b["id"] for b in upgrade["blocks"]] == [5]

    with contextlib.redirect_stdout(io.StringIO()):
        assert sr.apply() == 0

    assert task_snapshot(root) == first


def test_sample_apply_waits_for_non_sample_flagged_verdict_before_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(6)]
    sampled = [0, 2, 3, 5]
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        [verdict(i) for i in sampled],
        groups=sample_group(sampled, [4]),
        flagged={"1": "alnum"},
    )

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = sr.apply()

    assert rc == 0
    assert not (root / ".triage/verdicts/auto_sample.jsonl").exists()
    assert "抽样审查闭环" not in stdout.getvalue()


@pytest.mark.parametrize(
    ("sample_kind", "flag_kind"),
    [("ok", "suspect"), ("unsure", "unsure")],
)
def test_sample_apply_late_flagged_non_ok_revokes_auto_and_escalates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_kind: str,
    flag_kind: str,
):
    blocks = [block(i) for i in range(6)]
    sampled = [0, 2, 3, 5]
    records = [verdict(i, verdict=sample_kind if i == 0 else "ok") for i in sampled]
    records.append(verdict(1, verdict=flag_kind))
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        records,
        groups=sample_group(sampled, [4]),
        flagged={"1": "alnum"},
    )
    auto_path = root / ".triage/verdicts/auto_sample.jsonl"
    auto_path.write_text(
        json.dumps(verdict(4, cls="auto", reason="auto: 旧放行")) + "\n",
        encoding="utf-8",
    )

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 0
    assert not auto_path.exists() or all(
        json.loads(line)["id"] != 4
        for line in auto_path.read_text(encoding="utf-8").splitlines()
    )
    task = json.loads(
        (root / ".triage/review_chunks/task_00.json").read_text(encoding="utf-8")
    )
    assert [b["id"] for b in task["blocks"]] == [4]


def test_sample_plan_excludes_missing_original_from_alignment_and_locate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pr = load_review_module("prepare_review", monkeypatch)
    real_build_jp_text = pr.build_jp_text
    real_build_aligned_text = pr.build_aligned_text
    sr, root = sample_root(tmp_path, monkeypatch, [block(0)], [])
    (tmp_path / "index-jp").mkdir()
    aligned_dir = tmp_path / "aligned/S1_01"
    aligned_dir.mkdir(parents=True)
    (aligned_dir / "S1_01-01.xhtml").write_text("<p>陈旧对齐原文</p>", encoding="utf-8")
    monkeypatch.setattr(sr.pr, "build_jp_text", real_build_jp_text)
    monkeypatch.setattr(sr.pr, "build_aligned_text", real_build_aligned_text)
    monkeypatch.setattr(sr.pr, "aligned_vols", lambda root: {"S1_01": str(aligned_dir)})
    located_with = []

    def fake_locate(root, xdirs, aligned, block):
        located_with.append(aligned)

    monkeypatch.setattr(sr.pr, "locate_block", fake_locate)

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.plan()

    assert rc == 0
    assert not (root / ".triage/jp_text/S1_01.aligned.txt").exists()
    assert located_with == [{}]


def test_sample_apply_rebinds_current_jp_before_verdict_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pr = load_review_module("prepare_review", monkeypatch)
    real_build_jp_text = pr.build_jp_text
    blocks = [block(i) for i in range(5)]
    records = [verdict(0, cls="semantic", jp="陈旧原文")]
    records.extend(verdict(i) for i in range(1, 4))
    sr, root = sample_root(tmp_path, monkeypatch, blocks, records)
    monkeypatch.setattr(sr.pr, "build_jp_text", real_build_jp_text)
    stale = root / ".triage/jp_text/S1_01.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("陈旧原文", encoding="utf-8")
    page = tmp_path / "index-jp" / VOL / "item/xhtml/p001.xhtml"
    page.parent.mkdir(parents=True)
    page.write_text("<p>本轮原文</p>", encoding="utf-8")

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 1
    assert "本轮原文" in stale.read_text(encoding="utf-8")
    assert not (root / ".triage/verdicts/auto_sample.jsonl").exists()


def test_sample_plan_builds_jp_for_every_block_file_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pr = load_review_module("prepare_review", monkeypatch)
    real_build_jp_text = pr.build_jp_text
    cross_volume = block(0, files=[REL, REL2])
    sr, root = sample_root(tmp_path, monkeypatch, [cross_volume], [])
    (root / "index-X/EPUB" / VOL2).mkdir(parents=True)
    install_original(tmp_path, VOL, "第一卷原文")
    install_original(tmp_path, VOL2, "第二卷原文")
    monkeypatch.setattr(sr.pr, "build_jp_text", real_build_jp_text)

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.plan()

    assert rc == 0
    second_txt = root / ".triage/jp_text/S1_02.txt"
    assert second_txt.exists()
    prompt = (root / ".triage/review_prompts/task_00.md").read_text(encoding="utf-8")
    assert str(second_txt) in prompt


def test_sample_escalation_builds_jp_for_every_block_file_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pr = load_review_module("prepare_review", monkeypatch)
    real_build_jp_text = pr.build_jp_text
    blocks = [block(0), block(1, files=[REL, REL2])]
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        [verdict(0, verdict="suspect")],
        groups=sample_group([0], [1]),
    )
    (root / "index-X/EPUB" / VOL2).mkdir(parents=True)
    install_original(tmp_path, VOL, "第一卷原文")
    install_original(tmp_path, VOL2, "第二卷原文")
    monkeypatch.setattr(sr.pr, "build_jp_text", real_build_jp_text)

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 0
    second_txt = root / ".triage/jp_text/S1_02.txt"
    assert second_txt.exists()
    prompt = (root / ".triage/review_prompts/task_00.md").read_text(encoding="utf-8")
    assert str(second_txt) in prompt


def test_sample_apply_ignores_archived_task_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    sampled = [0, 1, 3, 4]
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        [verdict(i) for i in sampled],
        groups=sample_group(sampled, [2]),
    )
    install_task(root, 7, [blocks[2]])

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 0
    auto_path = root / ".triage/verdicts/auto_sample.jsonl"
    auto = [
        json.loads(line) for line in auto_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [v["id"] for v in auto] == [2]
    assert sorted(task_snapshot(root)) == ["task_07.json"]


def test_sample_apply_empty_blocks_is_side_effect_free_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sr = load_review_module("sample_review", monkeypatch)
    root = tmp_path / "repo"
    triage = root / ".triage"
    triage.mkdir(parents=True)
    write_json(triage / "review_blocks.json", [])
    write_json(triage / "review_changes.json", [])
    stale_jp = triage / "jp_text/stale.txt"
    stale_jp.parent.mkdir()
    stale_jp.write_text("保持不动", encoding="utf-8")
    monkeypatch.setattr(sr, "repo_root", lambda: str(root))
    before = {
        str(path.relative_to(triage)): path.read_bytes()
        for path in triage.rglob("*")
        if path.is_file()
    }

    with contextlib.redirect_stdout(io.StringIO()):
        rc = sr.apply()

    assert rc == 0
    after = {
        str(path.relative_to(triage)): path.read_bytes()
        for path in triage.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_sample_apply_completed_escalation_does_not_spin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocks = [block(i) for i in range(5)]
    sampled = [0, 1, 3, 4]
    records = [verdict(i, verdict="suspect" if i == 0 else "ok") for i in sampled]
    records.append(verdict(2))
    sr, root = sample_root(
        tmp_path,
        monkeypatch,
        blocks,
        records,
        groups=sample_group(sampled, [2]),
    )
    install_task(root, 7, [blocks[2]])
    plan_path = root / ".triage/sample_plan.json"
    plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_doc["escalation_tasks"] = [7]
    write_json(plan_path, plan_doc)
    before = task_snapshot(root)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = sr.apply()

    assert rc == 0
    assert "抽样审查闭环" in stdout.getvalue()
    assert "升级子波" not in stdout.getvalue()
    assert task_snapshot(root) == before
