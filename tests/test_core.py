import importlib.util
import shutil
import subprocess
import sys
import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VOLUME = "【S1_01】测试卷"
HEADER = "旧\t新\t注释\n"


def prepare_x2y_project(root: Path) -> Path:
    volume = root / "index-X" / "EPUB" / VOLUME
    volume.mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "rules" / "_common.tsv").write_text(HEADER, encoding="utf-8")
    sync = root / "scripts" / "sync"
    sync.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "sync" / "x2y.py", sync / "x2y.py")
    return volume


def import_x2y(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    lib_triage: types.ModuleType | None = None,
):
    name = f"x2y_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, root / "scripts/sync/x2y.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if lib_triage is None:
        lib_triage = types.ModuleType("lib_triage")
        lib_triage.__dict__["ensure_x"] = lambda: None
        lib_triage.__dict__["assert_x_clean"] = lambda: None
    monkeypatch.setitem(sys.modules, "lib_triage", lib_triage)
    spec.loader.exec_module(module)
    return module


def test_x2y_conversion_failure_preserves_old_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    volume = prepare_x2y_project(root)
    (volume / "broken.xhtml").write_bytes(b"<p>\xff</p>")
    old_y = root / "EPUB"
    old_y.mkdir()
    (old_y / "sentinel.txt").write_text("keep", encoding="utf-8")
    x2y = import_x2y(root, monkeypatch)

    with pytest.raises(UnicodeDecodeError):
        x2y.x2y()

    assert (old_y / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert not (old_y / VOLUME).exists()
    assert not list(root.glob(".EPUB.*"))

    (volume / "broken.xhtml").write_text("<p>fixed</p>", encoding="utf-8")
    x2y.x2y()

    assert not (old_y / "sentinel.txt").exists()
    assert (old_y / VOLUME / "broken.xhtml").read_text(
        encoding="utf-8"
    ) == "<p>fixed</p>"
    assert not list(root.glob(".EPUB.*"))


def test_x2y_interruption_after_backup_rename_restores_old_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    volume = prepare_x2y_project(root)
    (volume / "chapter.xhtml").write_text("new\n", encoding="utf-8")
    old_y = root / "EPUB"
    old_y.mkdir()
    (old_y / "sentinel.txt").write_text("sole old copy", encoding="utf-8")
    x2y = import_x2y(root, monkeypatch)
    real_replace = x2y.os.replace
    calls = 0

    def replace_then_interrupt(src, dst) -> None:
        nonlocal calls
        calls += 1
        real_replace(src, dst)
        if calls == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(x2y.os, "replace", replace_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        x2y.x2y()

    assert (old_y / "sentinel.txt").read_text(encoding="utf-8") == "sole old copy"
    assert not (old_y / VOLUME).exists()
    assert not list(root.glob(".EPUB.*"))


def test_x2y_import_does_not_require_x_or_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    sync = root / "scripts" / "sync"
    sync.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "sync" / "x2y.py", sync / "x2y.py")

    module = import_x2y(root, monkeypatch)

    assert module.ROOT == root


@pytest.mark.parametrize("dirty_kind", ["untracked", "tracked-dirty"])
def test_x2y_rejects_dirty_x_before_creating_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirty_kind: str
) -> None:
    root = tmp_path / "repo"
    volume = prepare_x2y_project(root)
    source = volume / "chapter.xhtml"
    source.write_text("HEAD content\n", encoding="utf-8")
    xrepo = root / "index-X"
    run_git(xrepo, "init", "-q")
    run_git(xrepo, "config", "user.name", "Test")
    run_git(xrepo, "config", "user.email", "test@example.com")
    run_git(xrepo, "add", "EPUB")
    run_git(xrepo, "commit", "-qm", "clean X")
    if dirty_kind == "untracked":
        (volume / "orphan.xhtml").write_text("orphan\n", encoding="utf-8")
    else:
        source.write_text("dirty content\n", encoding="utf-8")
    old_y = root / "EPUB"
    old_y.mkdir()
    (old_y / "sentinel.txt").write_text("keep", encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        f"lib_triage_test_{uuid.uuid4().hex}",
        PROJECT_ROOT / "scripts" / "sync" / "lib_triage.py",
    )
    assert spec is not None and spec.loader is not None
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    monkeypatch.setattr(lib, "repo_root", lambda: str(root))
    monkeypatch.setattr(lib, "ensure_x", lambda: None)
    x2y = import_x2y(root, monkeypatch, lib)

    with pytest.raises(RuntimeError, match="index-X/EPUB"):
        x2y.x2y()

    assert (old_y / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert not (old_y / VOLUME).exists()
    assert not list(root.glob(".EPUB.*"))


def test_rule_loading_rejects_invalid_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    prepare_x2y_project(root)
    (root / "rules" / "_common.tsv").write_text(
        HEADER + "a\t\\2\tinvalid group\n", encoding="utf-8"
    )
    x2y = import_x2y(root, monkeypatch)

    with pytest.raises(SystemExit, match="replacement|替换"):
        x2y.load_rule_records()


@pytest.mark.parametrize(
    "common",
    [HEADER, HEADER + "# disabled\told\tnote\n"],
    ids=["header-only", "comments-only"],
)
def test_fixed_accepts_common_section_without_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, common: str
) -> None:
    root = tmp_path / "repo"
    prepare_x2y_project(root)
    (root / "rules" / "_common.tsv").write_text(common, encoding="utf-8")
    x2y = import_x2y(root, monkeypatch)

    assert x2y.fixed(VOLUME, "<p>unchanged</p>") == "<p>unchanged</p>"


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def make_overlap_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "core.quotepath", "true")
    path = repo / "EPUB" / VOLUME / "OEBPS/Text/S1_01-01_中文.xhtml"
    path.parent.mkdir(parents=True)
    path.write_text("<p>旧中文</p>\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "old")
    path.write_text("<p>新中文</p>\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "new")
    return repo, run_git(repo, "rev-parse", "HEAD").strip()


def import_proofread_overlap():
    spec = importlib.util.spec_from_file_location(
        f"proofread_overlap_test_{uuid.uuid4().hex}",
        PROJECT_ROOT / "scripts/proofread_overlap.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_pairs_reads_unicode_paths_with_quotepath_enabled(
    tmp_path: Path,
) -> None:
    repo, head = make_overlap_repo(tmp_path)
    overlap = import_proofread_overlap()

    pairs = overlap.extract_pairs(repo, head, "S1_01")

    assert [(p.file, p.old, p.new) for p in pairs] == [
        ("S1_01-01_中文.xhtml", "<p>旧中文</p>", "<p>新中文</p>")
    ]


def test_current_corpus_reads_unicode_ls_tree_paths(tmp_path: Path) -> None:
    repo, _ = make_overlap_repo(tmp_path)
    overlap = import_proofread_overlap()
    overlap.X_REPO = repo

    corpus = overlap.current_corpus(f"EPUB/{VOLUME}")

    assert corpus == "新中文"


def make_edit_repo(root: Path, name: str, old: str, new: str) -> tuple[Path, str]:
    repo = root / name
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "user.email", "test@example.com")
    path = repo / "EPUB" / VOLUME / "OEBPS/Text/S1_01-01.xhtml"
    path.parent.mkdir(parents=True)
    path.write_text(old + "\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "old")
    path.write_text(new + "\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "new")
    return repo, run_git(repo, "rev-parse", "HEAD").strip()


def test_disjoint_edits_on_same_line_are_not_span_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    y_repo, y_head = make_edit_repo(tmp_path, "y", "<p>A B</p>", "<p>Y B</p>")
    x_repo, x_head = make_edit_repo(tmp_path, "x", "<p>A B</p>", "<p>A X</p>")
    overlap = import_proofread_overlap()
    monkeypatch.setattr(overlap, "Y_REPO", y_repo)
    monkeypatch.setattr(overlap, "X_REPO", x_repo)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "proofread_overlap.py",
            "S1_01",
            "--y-commit",
            y_head,
            "--x-commits",
            x_head,
        ],
    )

    overlap.main()

    assert "[0] 同行异改" in capsys.readouterr().out


def test_tag_push_retry_succeeds_at_same_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "user.email", "test@example.com")
    (repo / "book.txt").write_text("content", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "release")
    head = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "update-ref", "refs/remotes/origin/main", head)
    spec = importlib.util.spec_from_file_location(
        f"tag_test_{uuid.uuid4().hex}", PROJECT_ROOT / "scripts/tag.py"
    )
    assert spec is not None and spec.loader is not None
    tag_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tag_script)

    class FixedDatetime:
        @classmethod
        def now(cls):
            return datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    real_run = subprocess.run
    push_attempts = []
    published = set()

    def offline_run(command, *args, **kwargs):
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["git", "push"]:
            tag_name = command[-1]
            push_attempts.append(tag_name)
            if len(push_attempts) == 1:
                raise subprocess.CalledProcessError(1, command, stderr="rejected")
            published.add(tag_name)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["git", "ls-remote"]:
            tag_name = command[-1].rsplit("/", 1)[-1]
            stdout = f"refs/tags/{tag_name}\n" if tag_name in published else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        return real_run(command, *args, **kwargs)

    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["tag.py"])
    monkeypatch.setattr(tag_script, "datetime", FixedDatetime)
    monkeypatch.setattr(tag_script.subprocess, "run", offline_run)

    with pytest.raises(subprocess.CalledProcessError):
        tag_script.main()
    tag_script.main()

    assert len(push_attempts) == 2
    assert push_attempts[0] == push_attempts[1]
    assert published == {push_attempts[-1]}
    assert run_git(repo, "rev-parse", f"{push_attempts[-1]}^{{commit}}").strip() == head


def test_active_rules_first_point_skips_noop_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import colorama

    sync_package = types.ModuleType("sync")
    x2y_stub = types.ModuleType("sync.x2y")
    x2y_stub.__dict__["load_rule_records"] = list
    sync_package.__dict__["x2y"] = x2y_stub
    monkeypatch.setitem(sys.modules, "sync", sync_package)
    monkeypatch.setitem(sys.modules, "sync.x2y", x2y_stub)
    monkeypatch.setattr(colorama, "init", lambda: None)
    spec = importlib.util.spec_from_file_location(
        f"active_rules_test_{uuid.uuid4().hex}",
        PROJECT_ROOT / "scripts/active_rules.py",
    )
    assert spec is not None and spec.loader is not None
    active_rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(active_rules)
    active_rules.__dict__["X_DIR"] = tmp_path / "index-X" / "EPUB"
    path = active_rules.X_DIR / "V/OEBPS/Text/chapter.xhtml"
    path.parent.mkdir(parents=True)
    path.write_text("排名第一\n排占第二\n", encoding="utf-8")
    seq = [
        {
            "section": "*",
            "lineno": 1,
            "old": r"排(名)第一|排占第二",
            "new": r"排\1第一",
            "note": "",
        }
    ]

    active = active_rules.scan("V", [path], seq)

    assert active[0]["line"] == 2
    assert "排占第二" in active[0]["snip"]
