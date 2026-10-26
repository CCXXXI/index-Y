from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SYNC = REPO / "scripts" / "sync"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"{name}_{uuid.uuid4().hex}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_lib(root: Path, monkeypatch: pytest.MonkeyPatch):
    lib = load_module("test_sync_lib_triage", SYNC / "lib_triage.py")
    monkeypatch.setattr(lib, "repo_root", lambda: str(root))
    monkeypatch.setattr(lib, "STATE_DIR", str(root / ".triage"))
    monkeypatch.setitem(sys.modules, "lib_triage", lib)
    return lib


def git(cwd: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True
    ).stdout


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q")


def make_x_repo(root: Path) -> tuple[Path, Path, str]:
    init_repo(root)
    xrepo = root / "index-X"
    init_repo(xrepo)
    source = xrepo / "EPUB" / "Vol" / "chapter.xhtml"
    source.parent.mkdir(parents=True)
    source.write_text("HEAD content\n", encoding="utf-8")
    git(xrepo, "add", "EPUB/Vol/chapter.xhtml")
    git(
        xrepo,
        "-c",
        "user.name=sync-test",
        "-c",
        "user.email=sync-test@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    head = git(xrepo, "rev-parse", "HEAD").decode().strip()
    return xrepo, source, head


def load_jp_check(base: Path, monkeypatch: pytest.MonkeyPatch):
    root = base / "super"
    init_repo(root)
    aligned = base / "index-jp" / ".cache" / "bw_aligned"
    adir = aligned / "S1_01"
    adir.mkdir(parents=True)
    (aligned / "manifest.json").write_text(
        json.dumps({"S1_01": {"status": "pass"}}), encoding="utf-8"
    )
    (adir / "S1_01-1.xhtml").write_text("jp1\njp2\n", encoding="utf-8")
    load_lib(root, monkeypatch)
    jp_align = load_module("test_sync_jp_align", REPO / "scripts" / "jp_align.py")
    monkeypatch.setitem(sys.modules, "jp_align", jp_align)
    fake_rules = types.ModuleType("alignment_rules")
    fake_rules.__dict__["PAIR_RULES"] = {}
    fake_rules.__dict__["TEXTUAL_IMAGE_HEADERS"] = frozenset()
    monkeypatch.setitem(sys.modules, "alignment_rules", fake_rules)
    check = load_module("test_sync_jp_check", SYNC / "check_y_jp_alignment.py")
    monkeypatch.setattr(sys, "argv", [str(SYNC / "check_y_jp_alignment.py")])
    return root, check


def test_assert_x_clean_rejects_untracked_epub_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    xrepo, _, _ = make_x_repo(root)
    orphan = xrepo / "EPUB" / "Vol" / "orphan.xhtml"
    orphan.write_text("not in HEAD\n", encoding="utf-8")
    lib = load_lib(root, monkeypatch)

    with pytest.raises(RuntimeError, match="index-X/EPUB"):
        lib.assert_x_clean()


def test_finish_rejects_tracked_dirty_x_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    _, source, _ = make_x_repo(root)
    source.write_text("uncommitted content\n", encoding="utf-8")
    yfile = root / "EPUB" / "Vol" / "chapter.xhtml"
    yfile.parent.mkdir(parents=True)
    yfile.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    load_lib(root, monkeypatch)
    fake_x2y = types.ModuleType("x2y")
    fake_x2y.__dict__["fixed"] = lambda _vol, content: content
    monkeypatch.setitem(sys.modules, "x2y", fake_x2y)
    check = load_module("test_sync_dirty_x_check", SYNC / "check_y_freshness.py")
    monkeypatch.setattr(check, "ensure_x", lambda: None)
    monkeypatch.setattr(sys, "argv", [str(SYNC / "check_y_freshness.py")])
    run_all = load_module("test_sync_dirty_x_run_all", SYNC / "run_all.py")
    monkeypatch.setattr(
        run_all,
        "git_ok",
        lambda *args: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    committed = False

    def fake_run(script: str, *args: str) -> None:
        nonlocal committed
        if script == "check_y_freshness.py":
            rc = check.main()
            if rc:
                raise SystemExit(rc)
        elif script == "triage_text.py" and args == ("--commit",):
            committed = True

    monkeypatch.setattr(run_all, "run", fake_run)
    with pytest.raises(RuntimeError, match="index-X/EPUB"):
        run_all.finish()
    assert not committed


def test_update_x_rejects_head_mismatch_before_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    _, _, old_head = make_x_repo(root)
    lib = load_lib(root, monkeypatch)
    state = {"head": old_head}
    monkeypatch.setattr(lib, "x_head", lambda: state["head"])
    rendered = False
    fake_x2y = types.ModuleType("x2y")

    def render() -> None:
        nonlocal rendered
        rendered = True

    fake_x2y.__dict__["x2y"] = render
    monkeypatch.setitem(sys.modules, "x2y", fake_x2y)
    update = load_module("test_sync_update_x", SYNC / "update_x.py")
    monkeypatch.setattr(update, "ensure_x", lambda: None)
    monkeypatch.setattr(update, "pin_sha", lambda: old_head)
    monkeypatch.setattr(update, "x_head", lambda: state["head"])
    monkeypatch.setattr(update, "set_sync_ref", lambda _name: None)
    target = "b" * 40

    def fake_gitx(*args: str) -> bytes:
        if args[0] == "rev-parse":
            return f"{target}\n".encode()
        return b""

    monkeypatch.setattr(update, "gitx", fake_gitx)
    monkeypatch.setattr(
        sys, "argv", [str(SYNC / "update_x.py"), "--ref", "origin/master"]
    )
    with pytest.raises(RuntimeError, match="HEAD"):
        update.main()
    assert not rendered


def test_freshness_checks_full_tree_and_exposes_reusable_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    xrepo, source, _ = make_x_repo(root)
    yfile = root / "EPUB" / "Vol" / "chapter.xhtml"
    yfile.parent.mkdir(parents=True)
    yfile.write_bytes(source.read_bytes())
    x_image = xrepo / "EPUB" / "Vol" / "cover.png"
    x_image.write_bytes(b"new-image")
    git(xrepo, "add", "EPUB/Vol/cover.png")
    git(
        xrepo,
        "-c",
        "user.name=sync-test",
        "-c",
        "user.email=sync-test@example.invalid",
        "commit",
        "-q",
        "-m",
        "add image",
    )
    (root / "EPUB" / "Vol" / "cover.png").write_bytes(b"old-image")
    (root / "EPUB" / "Vol" / "orphan.xhtml").write_text("not in X\n", encoding="utf-8")
    lib = load_lib(root, monkeypatch)
    fake_x2y = types.ModuleType("x2y")
    fake_x2y.__dict__["fixed"] = lambda _vol, content: content
    monkeypatch.setitem(sys.modules, "x2y", fake_x2y)
    check = load_module("test_sync_tree_check", SYNC / "check_y_freshness.py")
    monkeypatch.setattr(check, "ensure_x", lambda: None)
    monkeypatch.setattr(sys, "argv", [str(SYNC / "check_y_freshness.py")])

    assert check.main() == 1

    (xrepo / "EPUB" / "Vol" / "missing.xhtml").write_text(
        "missing in Y\n", encoding="utf-8"
    )
    assert lib.freshness_errors(str(root), fake_x2y.fixed) == [
        ("Vol/cover.png", "二进制内容不一致"),
        ("Vol/missing.xhtml", "Y 缺失"),
        ("Vol/orphan.xhtml", "Y 额外"),
    ]


def test_freshness_matches_x2y_case_sensitive_text_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    for side in ("index-X/EPUB", "EPUB"):
        target = root / side / "Vol" / "upper.XHTML"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old\n", encoding="utf-8")
    lib = load_lib(root, monkeypatch)

    assert lib.freshness_errors(str(root), lambda _vol, _text: "new\n") == []


@pytest.mark.parametrize(
    "rules_pending", [False, True], ids=["no-rules", "delayed-rules"]
)
def test_jp_failure_blocks_atomic_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules_pending: bool
):
    root = tmp_path / "repo"
    root.mkdir()
    if rules_pending:
        rules = root / "rules"
        rules.mkdir()
        (rules / "_common.tsv").write_text("in-flight rules\n", encoding="utf-8")
    load_lib(root, monkeypatch)
    run_all = load_module("test_sync_jp_gate_run_all", SYNC / "run_all.py")

    def fake_git_ok(*args: str) -> subprocess.CompletedProcess:
        stdout = " M rules/_common.tsv\0" if rules_pending else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    committed = False

    def fail_jp(script: str, *args: str) -> None:
        nonlocal committed
        if script == "triage_text.py" and args == ("--commit",):
            committed = True
        elif script == "check_y_jp_alignment.py":
            raise SystemExit("fixture JP mismatch")

    monkeypatch.setattr(run_all, "git_ok", fake_git_ok)
    monkeypatch.setattr(run_all, "run", fail_jp)
    with pytest.raises(SystemExit, match="fixture JP mismatch"):
        run_all.finish()
    assert not committed


@pytest.mark.parametrize(
    "rules_pending", [False, True], ids=["no-rules", "dirty-rules"]
)
def test_stranded_rules_backup_blocks_finish_without_data_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules_pending: bool
):
    root = tmp_path / "repo"
    rules = root / "rules"
    rules.mkdir(parents=True)
    current = rules / "_common.tsv"
    current.write_text("current rules\n", encoding="utf-8")
    backup_file = root / ".triage" / "rules_backup" / "new.tsv"
    backup_file.parent.mkdir(parents=True)
    backup_file.write_text("sole copy of old in-flight rules\n", encoding="utf-8")
    load_lib(root, monkeypatch)
    run_all = load_module("test_sync_backup_guard", SYNC / "run_all.py")
    calls: list[str] = []

    def fake_git_ok(*args: str) -> subprocess.CompletedProcess:
        stdout = " M rules/_common.tsv\0" if rules_pending else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(run_all, "git_ok", fake_git_ok)
    monkeypatch.setattr(run_all, "run", lambda script, *args: calls.append(script))
    with pytest.raises(SystemExit, match="rules_backup"):
        run_all.finish()
    assert backup_file.read_text(encoding="utf-8") == (
        "sole copy of old in-flight rules\n"
    )
    assert current.read_text(encoding="utf-8") == "current rules\n"
    assert calls == []


@pytest.mark.parametrize("fail_stage", ["head-render", "jp", "commit"])
def test_interrupted_delayed_rules_restore_preserves_completed_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_stage: str
):
    root = tmp_path / "repo"
    rules = root / "rules"
    rules.mkdir(parents=True)
    current = rules / "_common.tsv"
    original = "sole copy of in-flight rules\n"
    current.write_text(original, encoding="utf-8")
    load_lib(root, monkeypatch)
    run_all = load_module("test_sync_backup_interruption", SYNC / "run_all.py")

    def fake_git_ok(*args: str) -> subprocess.CompletedProcess:
        stdout = " M rules/_common.tsv\0" if args[0] == "status" else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    render_calls = 0

    def interrupt(script: str, *args: str) -> None:
        nonlocal render_calls
        if script == "x2y.py":
            render_calls += 1
            if (fail_stage == "head-render" and render_calls == 1) or (
                render_calls >= 2
            ):
                raise SystemExit("fixture render interrupted")
        elif script == "check_y_jp_alignment.py" and fail_stage == "jp":
            raise SystemExit("fixture JP interrupted")
        elif (
            script == "triage_text.py"
            and args == ("--commit",)
            and fail_stage == "commit"
        ):
            raise SystemExit("fixture commit interrupted")

    monkeypatch.setattr(run_all, "git_ok", fake_git_ok)
    monkeypatch.setattr(run_all, "run", interrupt)
    with pytest.raises(SystemExit, match="fixture"):
        run_all.finish()

    backup = root / ".triage" / "rules_backup"
    assert current.read_text(encoding="utf-8") == original
    assert (backup / "_common.tsv").read_text(encoding="utf-8") == original
    assert (backup / ".complete").is_file()
    assert not (rules / ".complete").exists()


def test_interrupted_backup_creation_leaves_guarded_staging_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    rules = root / "rules"
    rules.mkdir(parents=True)
    current = rules / "_common.tsv"
    current.write_text("current in-flight rules\n", encoding="utf-8")
    load_lib(root, monkeypatch)
    run_all = load_module("test_sync_backup_creation", SYNC / "run_all.py")
    monkeypatch.setattr(
        run_all,
        "git_ok",
        lambda *args: subprocess.CompletedProcess(
            args, 0, stdout=" M rules/_common.tsv\0", stderr=""
        ),
    )
    monkeypatch.setattr(run_all, "run", lambda _script, *args: None)

    def interrupt_copy(_src, dst, *args, **kwargs) -> None:
        target = Path(dst)
        target.mkdir(parents=True)
        (target / "partial.tsv").write_text("partial backup\n", encoding="utf-8")
        raise OSError("fixture backup copy interrupted")

    monkeypatch.setattr(run_all.shutil, "copytree", interrupt_copy)
    with pytest.raises(OSError, match="backup copy interrupted"):
        run_all.finish()

    backup = root / ".triage" / "rules_backup"
    staging = root / ".triage" / "rules_backup.tmp"
    assert not backup.exists()
    assert (staging / "partial.tsv").read_text(encoding="utf-8") == ("partial backup\n")
    assert not (staging / ".complete").exists()
    assert current.read_text(encoding="utf-8") == "current in-flight rules\n"

    with pytest.raises(SystemExit, match="rules_backup"):
        run_all.finish()
    assert (staging / "partial.tsv").is_file()
    assert current.read_text(encoding="utf-8") == "current in-flight rules\n"


def test_sync_with_stranded_rules_backup_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    init_repo(root)
    backup = root / ".triage" / "rules_backup"
    backup.mkdir(parents=True)
    (backup / "new.tsv").write_text("sole copy\n", encoding="utf-8")
    load_lib(root, monkeypatch)
    run_all = load_module("test_sync_backup_update_guard", SYNC / "run_all.py")
    monkeypatch.setattr(run_all, "repo_root", lambda: str(root))
    monkeypatch.setattr(run_all, "x_head", lambda: "a" * 40)
    monkeypatch.setattr(run_all, "pin_sha", lambda: "a" * 40)
    monkeypatch.setattr(
        run_all.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="", stderr=""
        ),
    )
    calls = []
    monkeypatch.setattr(run_all, "run", lambda *args: calls.append(args))

    with pytest.raises(SystemExit, match="rules_backup"):
        run_all.update_x(None)
    assert calls == []


@pytest.mark.parametrize(
    "recognized_volume", [False, True], ids=["missing-volume", "zero-pairs"]
)
def test_jp_alignment_rejects_missing_pass_volume_and_zero_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recognized_volume: bool,
):
    root, check = load_jp_check(tmp_path, monkeypatch)
    volume = "【S1_01】fixture" if recognized_volume else "【卷一】new-name"
    (root / "EPUB" / volume / "OEBPS" / "Text").mkdir(parents=True)

    assert check.main() == 1
    assert "S1_01" in capsys.readouterr().out


def test_jp_alignment_rejects_missing_numeric_unit_with_matched_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    root, check = load_jp_check(tmp_path, monkeypatch)
    ytext = root / "EPUB" / "【S1_01】fixture" / "OEBPS" / "Text"
    ytext.mkdir(parents=True)
    (ytext / "S1_01-1_correct.xhtml").write_text("y1\ny2\n", encoding="utf-8")
    aligned = root.parent / "index-jp" / ".cache" / "bw_aligned" / "S1_01"
    (aligned / "S1_01-2.xhtml").write_text("missing\n", encoding="utf-8")

    assert check.main() == 1
    assert "S1_01-2" in capsys.readouterr().out


def test_jp_alignment_allows_registered_numeric_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, check = load_jp_check(tmp_path, monkeypatch)
    ytext = root / "EPUB" / "【S1_01】fixture" / "OEBPS" / "Text"
    ytext.mkdir(parents=True)
    (ytext / "S1_01-1_correct.xhtml").write_text("y1\ny2\n", encoding="utf-8")
    aligned = root.parent / "index-jp" / ".cache" / "bw_aligned" / "S1_01"
    (aligned / "S1_01-08.xhtml").write_text("author profile\n", encoding="utf-8")

    assert check.main() == 0


def test_jp_alignment_uses_exact_unit_code_on_prefix_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    root, check = load_jp_check(tmp_path, monkeypatch)
    ytext = root / "EPUB" / "【S1_01】fixture" / "OEBPS" / "Text"
    ytext.mkdir(parents=True)
    wrong = ytext / "S1_01-10_other.xhtml"
    correct = ytext / "S1_01-1_correct.xhtml"
    wrong.write_text("wrong1\nwrong2\n", encoding="utf-8")
    correct.write_text("correct1\ncorrect2\ncorrect3\n", encoding="utf-8")
    real_listdir = check.os.listdir

    def ordered_listdir(path):
        if Path(path) == ytext:
            return [wrong.name, correct.name]
        return real_listdir(path)

    monkeypatch.setattr(check.os, "listdir", ordered_listdir)
    assert check.main() == 1
    assert correct.name in capsys.readouterr().out


def test_jp_alignment_skips_unpaired_nonnumeric_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, check = load_jp_check(tmp_path, monkeypatch)
    ytext = root / "EPUB" / "【S1_01】fixture" / "OEBPS" / "Text"
    ytext.mkdir(parents=True)
    (ytext / "S1_01-1_correct.xhtml").write_text("y1\ny2\n", encoding="utf-8")
    aligned = root.parent / "index-jp" / ".cache" / "bw_aligned" / "S1_01"
    (aligned / "S1_01-fm01.xhtml").write_text("front matter\n", encoding="utf-8")

    assert check.main() == 0
