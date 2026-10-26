from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VOL = "【S1_01】fixture"


def load_module(
    name: str, relative: str, monkeypatch: pytest.MonkeyPatch, *, stub_x2y: bool = False
):
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    if stub_x2y:
        sync_pkg = types.ModuleType("sync")
        sync_pkg.__path__ = []
        sync_x2y = types.ModuleType("sync.x2y")
        sync_x2y.__dict__["load_rule_records"] = list
        sync_pkg.__dict__["x2y"] = sync_x2y
        monkeypatch.setitem(sys.modules, "sync", sync_pkg)
        monkeypatch.setitem(sys.modules, "sync.x2y", sync_x2y)
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def load_rule_audit(monkeypatch: pytest.MonkeyPatch):
    return load_module(
        "test_proof_rule_jp_audit",
        "scripts/rule_jp_audit.py",
        monkeypatch,
        stub_x2y=True,
    )


def apply_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    x_text: str,
    target_rows: list[str],
    candidate_rows: list[str],
    common_rows: list[str] | None = None,
):
    root = tmp_path / "repo"
    rel = Path("OEBPS/Text/S1_01-01.xhtml")
    source = root / "index-X/EPUB" / VOL / rel
    source.parent.mkdir(parents=True)
    source.write_text(x_text, encoding="utf-8")
    rules_dir = root / "rules"
    rules_dir.mkdir()
    common = ["旧\t新\t注释", "不存在\t存在\t", *(common_rows or [])]
    (rules_dir / "_common.tsv").write_text("\n".join(common) + "\n", encoding="utf-8")
    target = rules_dir / f"{VOL}.tsv"
    target.write_text(
        "\n".join(["旧\t新\t注释", *target_rows]) + "\n", encoding="utf-8"
    )
    candidates = root / "candidates.tsv"
    candidates.write_text(
        "\n".join(["旧\t新\t注释", *candidate_rows]) + "\n", encoding="utf-8"
    )
    sync_dir = root / "scripts/sync"
    sync_dir.mkdir(parents=True)
    shutil.copyfile(REPO / "scripts/sync/x2y.py", sync_dir / "x2y.py")
    lib_triage = types.ModuleType("lib_triage")
    lib_triage.__dict__["ensure_x"] = lambda: None
    monkeypatch.setitem(sys.modules, "lib_triage", lib_triage)
    module = load_module(
        "test_proof_apply_rules",
        "scripts/agent_proofread/apply_rules.py",
        monkeypatch,
    )
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "RULES_DIR", rules_dir)
    monkeypatch.setattr(
        module, "__file__", str(root / "scripts/agent_proofread/apply_rules.py")
    )
    monkeypatch.setattr(sys, "argv", ["apply_rules.py", "S1_01", str(candidates)])
    return module, target


def findings_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    y_text: str,
    findings: list[dict],
    x_text: str | None = None,
    jp_text: str = "<p>原文です</p>\n",
):
    root = tmp_path / "repo"
    rel = Path("OEBPS/Text/S1_01-01.xhtml")
    for side in ("index-X/EPUB", "EPUB"):
        source = root / side / VOL / rel
        source.parent.mkdir(parents=True)
        text = x_text if side == "index-X/EPUB" and x_text is not None else y_text
        source.write_text(text, encoding="utf-8", newline="\n")
    jp_root = root / "jp"
    jp_file = jp_root / VOL / "OEBPS/Text/S1_01-01.xhtml"
    jp_file.parent.mkdir(parents=True)
    jp_file.write_text(jp_text, encoding="utf-8", newline="\n")
    findings_path = root / "findings.json"
    findings_path.write_text(json.dumps(findings, ensure_ascii=False), encoding="utf-8")
    out_path = root / "verified.json"
    module = load_module(
        "test_proof_verify_findings",
        "scripts/agent_proofread/verify_findings.py",
        monkeypatch,
    )
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_findings.py",
            "S1_01",
            str(findings_path),
            "--jp-root",
            str(jp_root),
            "-o",
            str(out_path),
        ],
    )
    return module, out_path


@pytest.mark.parametrize(
    ("old", "text"),
    [
        ("foo|bar", "<p>bar</p>\n"),
        ("(?<!函)授与", "<p>授与</p>\n"),
        ("<p>甲</p>\\n<p>乙</p>", "<p>甲</p>\n<p>乙</p>\n"),
    ],
    ids=["alternation", "lookaround", "cross-line"],
)
def test_rule_scan_uses_safe_prefilter_and_full_text_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, text: str
):
    audit = load_rule_audit(monkeypatch)
    x_dir = tmp_path / "X"
    source = x_dir / VOL / "OEBPS/Text/S1_01-01.xhtml"
    source.parent.mkdir(parents=True)
    source.write_text(text, encoding="utf-8")
    monkeypatch.setattr(audit, "X_DIR", x_dir)
    rule: dict = {"old": old, "section": VOL}

    audit.scan_hits([rule])

    assert rule["core"] == ""
    assert [(h["rel"], h["line"], h["text"]) for h in rule["hit_list"]] == [
        ("OEBPS/Text/S1_01-01.xhtml", 1, text.splitlines()[0])
    ]


@pytest.mark.parametrize(
    ("unit_count", "verdicts"),
    [
        (2, []),
        (2, [{"id": "S1_01:1", "verdict": "ok"}]),
        (
            1,
            [
                {"id": "S1_01:1", "verdict": "ok"},
                {"id": "S1_01:1", "verdict": "ok"},
            ],
        ),
        (
            1,
            [
                {"id": "S1_01:1", "verdict": "ok"},
                {"id": "S1_01:unknown", "verdict": "ok"},
            ],
        ),
    ],
    ids=["empty", "missing", "duplicate", "unknown"],
)
def test_audit_verify_rejects_invalid_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unit_count: int,
    verdicts: list[dict],
):
    audit = load_rule_audit(monkeypatch)
    monkeypatch.setattr(audit, "JP_ROOT", tmp_path / "jp")
    units_path = tmp_path / "units.json"
    verdicts_path = tmp_path / "verdicts.json"
    units_path.write_text(
        json.dumps(
            [{"id": f"S1_01:{n}", "samples": []} for n in range(1, unit_count + 1)]
        ),
        encoding="utf-8",
    )
    verdicts_path.write_text(json.dumps(verdicts), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        audit.cmd_verify(units_path, verdicts_path)

    assert exc_info.value.code != 0


def test_audit_verify_rejects_empty_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    audit = load_rule_audit(monkeypatch)
    monkeypatch.setattr(audit, "JP_ROOT", tmp_path / "jp")
    units_path = tmp_path / "units.json"
    verdicts_path = tmp_path / "verdicts.json"
    units_path.write_text("[]", encoding="utf-8")
    verdicts_path.write_text('{"verdicts": []}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        audit.cmd_verify(units_path, verdicts_path)

    assert exc_info.value.code != 0
    output = capsys.readouterr().out
    assert "units 为空" in output
    assert "复核全部通过" not in output


def test_apply_rules_preserves_non_rule_lines_in_original_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, target = apply_fixture(
        tmp_path,
        monkeypatch,
        x_text="<p>甲</p>\n",
        target_rows=["# 卷级说明\t\t", "旧\t新\t", "# 尾部说明\t\t"],
        candidate_rows=["甲\t乙\t"],
    )

    assert module.main() == 0

    assert target.read_text(encoding="utf-8") == (
        "旧\t新\t注释\n# 卷级说明\t\t\n旧\t新\t\n# 尾部说明\t\t\n甲\t乙\t\n"
    )


def test_apply_rules_real_validation_failure_preserves_target_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, target = apply_fixture(
        tmp_path,
        monkeypatch,
        x_text="<p>#甲</p>\n",
        target_rows=["# 卷级说明\t\t"],
        candidate_rows=["#甲\t乙\t"],
    )
    before = target.read_bytes()

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code != 0
    assert target.read_bytes() == before


def test_apply_rules_ignores_disabled_common_rows_in_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, target = apply_fixture(
        tmp_path,
        monkeypatch,
        x_text="<p>甲</p>\n",
        target_rows=[],
        candidate_rows=["甲\t# 禁用\t"],
        common_rows=["# 禁用\tX\t"],
    )

    assert module.main() == 0
    assert "甲\t# 禁用\t\n" in target.read_text(encoding="utf-8")


def test_apply_rules_regex_guard_rejects_count_only_coincidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, target = apply_fixture(
        tmp_path,
        monkeypatch,
        x_text="<p>a+</p>\n",
        target_rows=[],
        candidate_rows=["a+\tZ\t"],
    )
    before = target.read_bytes()

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code != 0
    assert target.read_bytes() == before


def test_findings_rejects_empty_quote_and_whitespace_jp_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, out_path = findings_fixture(
        tmp_path,
        monkeypatch,
        y_text="<p>甲乙</p>\n<p>丙丁</p>\n",
        findings=[
            {
                "file": "S1_01-01.xhtml",
                "line": 1,
                "quote": "",
                "jp_evidence": " \n\t",
            }
        ],
    )

    assert module.main() == 0
    verified = json.loads(out_path.read_text(encoding="utf-8"))[0]
    assert verified["quote_ok"] is False
    assert verified["found_at"] is None
    assert verified["jp_ok"] is False


@pytest.mark.parametrize("verdict", ["wrong", "stale_evidence"])
def test_audit_verify_rejects_whitespace_only_required_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
):
    audit = load_rule_audit(monkeypatch)
    monkeypatch.setattr(audit, "JP_ROOT", tmp_path / "jp")
    units_path = tmp_path / "units.json"
    verdicts_path = tmp_path / "verdicts.json"
    units_path.write_text(
        json.dumps(
            [
                {
                    "id": "S1_01:1",
                    "samples": [{"vol": "S1_01", "jp": [{"L": 1, "text": "原文です"}]}],
                }
            ]
        ),
        encoding="utf-8",
    )
    verdicts_path.write_text(
        json.dumps([{"id": "S1_01:1", "verdict": verdict, "jp_evidence": " \n"}]),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        audit.cmd_verify(units_path, verdicts_path)

    assert exc_info.value.code != 0


def test_audit_citations_cannot_span_corpus_file_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    audit = load_rule_audit(monkeypatch)
    for vol, text in ((VOL, "<p>断片あ</p>"), ("【S1_02】fixture", "<p>断片い</p>")):
        source = tmp_path / "jp" / vol / "OEBPS/Text/source.xhtml"
        source.parent.mkdir(parents=True)
        source.write_text(text, encoding="utf-8")
    monkeypatch.setattr(audit, "JP_ROOT", tmp_path / "jp")

    blob_no_rt, blob_rt = audit.build_jp_blobs()

    assert (
        audit.check_citation("断片あ断片い", "quoted", blob_no_rt, blob_rt)["found"]
        is False
    )


def test_findings_jp_evidence_cannot_span_corpus_file_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, out_path = findings_fixture(
        tmp_path,
        monkeypatch,
        y_text="<p>甲乙</p>\n",
        findings=[
            {
                "file": "S1_01-01.xhtml",
                "line": 1,
                "quote": "甲乙",
                "jp_evidence": "断片あ断片い",
            }
        ],
        jp_text="<p>断片あ</p>\n",
    )
    second = out_path.parent / "jp" / VOL / "OEBPS/Text/S1_01-02.xhtml"
    second.write_text("<p>断片い</p>\n", encoding="utf-8")

    assert module.main() == 0
    verified = json.loads(out_path.read_text(encoding="utf-8"))[0]
    assert verified["jp_ok"] is False


def test_locator_side_y_uses_only_y_hits_for_jp_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    root = tmp_path / "repo"
    rel = Path("OEBPS/Text/S1_01-01.xhtml")
    for side, text in (("index-X/EPUB", "<p>旧词</p>\n"), ("EPUB", "<p>新词</p>\n")):
        source = root / side / VOL / rel
        source.parent.mkdir(parents=True)
        source.write_text(text, encoding="utf-8", newline="\n")
    aligned = root / "jp/.cache/bw_aligned/S1_01"
    aligned.mkdir(parents=True)
    (aligned.parent / "manifest.json").write_text(
        json.dumps({"S1_01": {"status": "pass"}}), encoding="utf-8"
    )
    (aligned / "S1_01-01.xhtml").write_text("<p>日本語</p>\n", encoding="utf-8")
    needles = root / "needles.txt"
    needles.write_text("新词\n旧词\n", encoding="utf-8")
    module = load_module(
        "test_proof_locator", "scripts/proofreading_locate.py", monkeypatch
    )
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "proofreading_locate.py",
            "S1_01",
            str(needles),
            "--jp-root",
            str(root / "jp"),
            "--side",
            "y",
        ],
    )

    module.main()
    output = capsys.readouterr().out

    new_section, old_section = output.split("===== 旧词")
    assert "===== 新词" in new_section
    assert "[JP直读] S1_01-01 L1 :: 日本語" in new_section
    assert "[JP直读]" not in old_section


def test_findings_supports_raw_and_stripped_cross_line_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, out_path = findings_fixture(
        tmp_path,
        monkeypatch,
        y_text="<p>甲<rt>A</rt>乙</p>\n<p>丙丁</p>\n<p>戊己</p>\n",
        findings=[
            {
                "file": "S1_01-01.xhtml",
                "line": 1,
                "quote": "丙丁</p>\n<p>戊己",
            },
            {
                "file": "S1_01-01.xhtml",
                "line": 1,
                "quote": "甲A乙丙丁",
            },
        ],
    )

    assert module.main() == 0
    verified = json.loads(out_path.read_text(encoding="utf-8"))
    assert [f["quote_ok"] for f in verified] == [True, True]
    assert [f["found_at"] for f in verified] == [2, 1]
    assert [f["found_end"] for f in verified] == [3, 2]


def test_findings_cross_line_comparison_covers_every_physical_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module, out_path = findings_fixture(
        tmp_path,
        monkeypatch,
        y_text="<p>甲乙</p>\n<p>丙丁</p>\n",
        x_text="<p>甲乙</p>\n<p>丙不同的丁</p>\n",
        findings=[
            {
                "file": "S1_01-01.xhtml",
                "line": 1,
                "quote": "甲乙</p>\n<p>丙丁",
            }
        ],
    )

    assert module.main() == 0
    verified = json.loads(out_path.read_text(encoding="utf-8"))[0]
    assert verified["quote_ok"] is True
    assert (verified["found_at"], verified["found_end"]) == (1, 2)
    assert verified["x_same"] is False


def test_plan_chunks_uses_physical_lines_and_emits_no_empty_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = load_module(
        "test_proof_plan_chunks",
        "scripts/agent_proofread/plan_chunks.py",
        monkeypatch,
    )
    source = tmp_path / "source.xhtml"
    source.write_text("<p>xx</p>\n", encoding="utf-8", newline="\n")
    blank = tmp_path / "blank.xhtml"
    blank.write_text("\n", encoding="utf-8", newline="\n")

    chunks = module.plan([source, blank], target=1)

    assert len(chunks) == 1
    assert chunks[0].chars > 0
    assert [(p.start, p.end, p.total) for p in chunks[0].parts] == [(1, 1, 1)]


def test_plan_chunks_documents_soft_chunk_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    module = load_module(
        "test_proof_plan_chunks_help",
        "scripts/agent_proofread/plan_chunks.py",
        monkeypatch,
    )
    monkeypatch.setattr(sys, "argv", ["plan_chunks.py", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "软上限" in module.__doc__
    assert "40000" in module.__doc__
    assert "软上限" in help_text
    assert "40000" in help_text
