"""上游上下文导出：commit 记录 + maintenance-records 命中预注，供审查参考。

参考源是同级目录 ../index-X 的本地 clone（全程经 git plumbing 读
origin/master，不依赖其工作区状态；clone 缺失时静默跳过并清除陈旧产物）。
记录是上游自报证据，【不免检】：本脚本只做机械层（基准
commit 定位、片段命中检索），「引文是否真支持改动」的语义判断保留给审查者
（.agents/skills/sync-triage/SKILL.md §6 上游记录的使用纪律）。

产物（.triage/ 下）：
- upstream_context.txt：clone 状态、逐卷基准匹配、本轮新 commit 列表
  （含 body）、范围内新增/修改的 maintenance-records 全文、全部记录索引。
- review_changes.txt / suspect_changes.txt 块尾预注（★ 前缀行）：块的最小
  差异片段在全部 records 中检索的命中（prepare_review 的 parse_blocks 跳过）。

基准匹配利用 git tree 哈希的内容寻址：epub 解压树与上游 EPUB/ 目录逐字节
一致（已实测），HEAD:X/<vol> 与上游某 commit 的 :EPUB/<vol> tree 相等即
命中。上游 push 可能晚于 zip 发布：旧侧匹配不到时按上次同步日期近似列举，
新侧不一致时报告「git 滞后于 zip」，两种情况 commit/记录都可能不全。
--fetch：先 git fetch（起新轮时 run_all 带入）；在途重跑不带，离线幂等。
用法: uv run python scripts/sync/upstream_context.py [--fetch]
"""

import difflib
import os
import re
import subprocess
import sys
from functools import partial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commit_image_renames import STATE_DIR
from lib_triage import ENV, git, repo_root, triage_parser

RECORDS_DIR = "docs/maintenance-records"
MAX_HIT_RECS = 4    # 每块最多列出的命中记录数
MAX_HIT_LINES = 2   # 每条记录最多列出的命中行数
MAX_LINE = 160      # 命中行截断
BLOCK_HEAD = re.compile(r"^(\[\d+次]|\[[XY]\]) ")


def clone_dir() -> str | None:
    """../index-X 存在且是 git 仓库时返回其路径，否则 None。"""
    d = os.path.normpath(os.path.join(repo_root(), "..", "index-X"))
    r = subprocess.run(["git", "-C", d, "rev-parse", "--git-dir"],
                       capture_output=True, env=ENV, check=False)
    return d if r.returncode == 0 else None


def gitx(clone: str, *args: str) -> bytes:
    r = subprocess.run(["git", "-C", clone, *args], capture_output=True,
                       env=ENV, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git -C index-X {' '.join(args)}: "
                           f"{r.stderr.decode('utf-8', 'replace')[:500]}")
    return r.stdout


def vol_tree(repo_git, ref: str, prefix: str, vol: str) -> str | None:
    """ref 下 <prefix>/<vol> 目录的 tree sha；不存在返回 None。"""
    out = repo_git("ls-tree", ref, "--", f"{prefix}/{vol}").decode().split()
    return out[2] if len(out) >= 3 and out[1] == "tree" else None


def changed_vols() -> list[str]:
    """工作区（含暂存）有在途改动的 X 侧卷名。"""
    out = git("status", "--porcelain", "-z").decode("utf-8")
    return sorted({ent[3:].strip('"').split("/")[1]
                   for ent in out.split("\0")
                   if ent and ent[3:].strip('"').startswith("X/")})


def touching_commits(clone: str, ref: str, vol: str) -> list[str]:
    """上游 master 上改动过 EPUB/<vol> 的 commit（新→旧）。"""
    out = gitx(clone, "log", "--format=%H", ref, "--",
               f"EPUB/{vol}").decode("utf-8")
    return [l for l in out.splitlines() if l]


def find_base(clone: str, ref: str, vol: str, old_tree: str) -> str | None:
    """最新一个 EPUB/<vol> tree == old_tree 的上游 commit（本轮基准）。"""
    gx = partial(gitx, clone)
    for sha in touching_commits(clone, ref, vol):
        if vol_tree(gx, sha, "EPUB", vol) == old_tree:
            return sha
    return None


def commit_brief(clone: str, sha: str) -> tuple[str, str, str]:
    """(短 sha, 日期, subject+body)。body 原样保留（上游的差异摘要）。"""
    out = gitx(clone, "show", "-s", "--format=%h %cI%n%B", sha).decode("utf-8")
    first, _, body = out.partition("\n")
    short, _, date = first.partition(" ")
    return short, date, body.rstrip("\n")


def match_vols(clone: str, ref: str, vols: list[str]):
    """逐卷基准匹配。返回 (行报告, 基准 sha 列表, {vol: [(sha, brief)]})。"""
    lines, bases, commits = [], [], {}
    if vols:
        git("add", "-A")  # 与 triage 各步一致：以暂存态为新侧
        staged_root = git("write-tree").decode().strip()
    for vol in vols:
        old = vol_tree(git, "HEAD", "X", vol)
        if old is None:
            lines.append(f"[新卷] {vol}（HEAD 无此卷；上游最新相关 commit 供参考）")
            commits[vol] = [(s, commit_brief(clone, s))
                            for s in touching_commits(clone, ref, vol)[:3]]
            continue
        base = find_base(clone, ref, vol, old)
        if base is None:
            date = git("log", "-1", "--format=%cI", "HEAD", "--",
                       f"X/{vol}").decode("utf-8").strip()
            near = [s for s in touching_commits(clone, ref, vol)
                    if commit_brief(clone, s)[1] > date]
            lines.append(f"[近似] {vol}：git 中找不到与旧 X 逐字一致的 commit"
                         f"（上轮 zip 领先 push？），按上次同步日期 {date[:10]}"
                         " 近似列举，可能不全")
            commits[vol] = [(s, commit_brief(clone, s)) for s in near]
            continue
        bases.append(base)
        touch = touching_commits(clone, ref, vol)
        new_shas = touch[:touch.index(base)]  # base 由 touching 列表得来，必在
        new_tree = vol_tree(git, staged_root, "X", vol)
        gx = partial(gitx, clone)
        lag = "" if vol_tree(gx, ref, "EPUB", vol) == new_tree else (
            "；⚠ origin/master 与 zip 新内容不一致（上游 push 晚于 zip："
            "commit/记录可能不全）")
        lines.append(f"[OK] {vol}：基准 {base[:8]}，新 commit {len(new_shas)} 条{lag}")
        commits[vol] = [(s, commit_brief(clone, s)) for s in new_shas]
    return lines, bases, commits


def records_index(clone: str, ref: str) -> list[str]:
    out = gitx(clone, "ls-tree", "-r", "--name-only", ref, "--",
               RECORDS_DIR).decode("utf-8")
    return [l for l in out.splitlines() if l.endswith(".md")]


def record_text(clone: str, ref: str, path: str) -> str:
    return gitx(clone, "show", f"{ref}:{path}").decode("utf-8")


def oldest_base(clone: str, ref: str, bases: list[str]) -> str:
    return max(bases, key=lambda b: int(
        gitx(clone, "rev-list", "--count", f"{b}..{ref}")))


def changed_records(clone: str, ref: str, bases: list[str]) -> list[str]:
    """范围内（最早基准..master）新增/修改的 record 路径。"""
    if not bases:
        return []
    out = gitx(clone, "log", "--format=", "--name-status",
               f"{oldest_base(clone, ref, bases)}..{ref}", "--",
               RECORDS_DIR).decode("utf-8")
    seen: list[str] = []
    for l in out.splitlines():
        if l and l[0] in "AM" and "\t" in l:
            p = l.split("\t", 1)[1]
            if p not in seen:
                seen.append(p)
    return seen


def policy_changes(clone: str, ref: str, bases: list[str]) -> list[str]:
    if not bases:
        return []
    out = gitx(clone, "log", "--format=", "--name-only",
               f"{oldest_base(clone, ref, bases)}..{ref}", "--",
               "AGENTS.md", ".agents").decode("utf-8")
    return sorted({l for l in out.splitlines() if l})


def windows_of(old: str, new: str) -> set[str]:
    """覆盖改动点的全部子串（新旧两侧，改动点 ±8 上下文内，长 3..17）。

    检索键要同时命中两种引用形态：整句引用（键 ⊆ 记录行）与术语引用
    （记录行中的术语 ⊆ 键）——char 级 difflib 差异常塌缩成 1-2 字
    （空中分【裂】→空中解【体】），固定窗口两头都够不着；改为「与 diff
    区间相交的全部子串」，术语（处置室）与整句（…在空中解体后…）自然都在内。
    """
    out = set()
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # 空跨度的侧（insert 的 old 侧 / delete 的 new 侧）跳过：生成的候选
        # 不含任何改动字符，只是纯上下文（如「在空中」），是误命中来源
        sides = [(old, i1, i2)] if i2 > i1 else []
        if j2 > j1:
            sides.append((new, j1, j2))
        for text, a, b in sides:
            for s in range(max(0, a - 8), a + 1):
                for e in range(max(b, s + 3), min(len(text), b + 8) + 1):
                    out.add(text[s:e])
    return out


def search_records(corpus: dict[str, str], frags: set[str]) -> list[str]:
    """片段在全部 records 中的命中行（★ 预注行列表，限量截断）。"""
    hits, n_rec = [], 0
    for path, text in corpus.items():
        if n_rec >= MAX_HIT_RECS:
            break
        matched = [l.strip()[:MAX_LINE] for l in text.splitlines()
                   if any(f in l for f in frags)]
        if matched:
            n_rec += 1
            hits += [f"★ {os.path.basename(path)}: {l}"
                     for l in matched[:MAX_HIT_LINES]]
    return hits


def annotate(path: str, corpus: dict[str, str]) -> int:
    """在 review/suspect 材料每块尾部插入 ★ 预注行；返回预注块数。

    行级遍历保持原格式：块 = [N次]/[X|Y] 头行 + -/+ 行与续行，空行收尾。
    triage_text 每次导出都覆写原材料，故预注天然幂等（材料更新后重跑本脚本）。
    """
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out: list[str] = []
    n_annotated = 0
    old: list[str] = []
    new: list[str] = []
    side: list[str] | None = None

    def flush() -> None:
        nonlocal n_annotated
        hits = search_records(corpus, windows_of("\n".join(old), "\n".join(new)))
        out.extend(hits)
        n_annotated += bool(hits)
        old.clear()
        new.clear()

    for line in lines:
        if BLOCK_HEAD.match(line):
            side = None
        elif line.startswith("- "):
            old.append(line[2:])
            side = old
        elif line.startswith("+ "):
            new.append(line[2:])
            side = new
        elif line and side is not None:
            side.append(line)
        elif not line:
            flush()
            side = None
        out.append(line)
    flush()
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return n_annotated


def main() -> int:
    parser = triage_parser(__doc__)
    parser.add_argument("--fetch", action="store_true",
                        help="先 git fetch ../index-X（起新轮时用）")
    args = parser.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    ctx_path = os.path.join(STATE_DIR, "upstream_context.txt")

    clone = clone_dir()
    if clone is None:
        if os.path.exists(ctx_path):
            os.remove(ctx_path)  # 防陈旧文件被误当本轮材料
        print("../index-X 不存在，跳过上游上下文导出")
        return 0
    if args.fetch:
        print("fetch ../index-X ...", flush=True)
        gitx(clone, "fetch", "--quiet", "origin")
    ref = gitx(clone, "rev-parse", "origin/master").decode("utf-8").strip()

    vols = changed_vols()
    lines, bases, commits = match_vols(clone, ref, vols)
    recs = records_index(clone, ref)
    corpus = {p: record_text(clone, ref, p) for p in recs}
    new_recs = changed_records(clone, ref, bases)
    policies = policy_changes(clone, ref, bases)

    w = [f"# 上游上下文（clone: ../index-X @ origin/master {ref[:8]}）",
         "# 记录是上游自报证据，不免检：机械比对已做，",
         "# 「引文→改动」的语义支持关系仍需审查者逐条判断。",
         "", "## 基准匹配"]
    w += lines or ["（无在途改动卷）"]
    w += ["", "## 本轮新 commit"]
    any_commit = False
    for vol in vols:
        items = commits.get(vol) or []
        if not items:
            continue
        any_commit = True
        w.append(f"### {vol}")
        for _sha, (short, date, body) in items:
            w.append(f"{short} {date[:10]} {body}")
            w.append("")
    if not any_commit:
        w.append("（无）")
    w += ["", f"## maintenance-records 变更（范围内 {len(new_recs)} 篇）"]
    for p in new_recs:
        w += [f"===== {p} =====", corpus.get(p, ""), ""]
    w += ["", "## 全部记录索引（块级 ★ 预注的检索范围）"]
    w += [f"- {p}" for p in recs]
    if policies:
        w += ["", "## 政策文件变更（AGENTS.md/.agents，范围内）"]
        w += [f"- {p}" for p in policies]
    with open(ctx_path, "w", encoding="utf-8") as f:
        f.write("\n".join(w) + "\n")
    print(f"上游上下文: {ctx_path}"
          f"（{len(vols)} 卷，新 commit {sum(len(v) for v in commits.values())} 条，"
          f"records 变更 {len(new_recs)} 篇）")

    for name in ("review_changes.txt", "suspect_changes.txt"):
        n = annotate(os.path.join(STATE_DIR, name), corpus)
        if n:
            print(f"{name}: {n} 块命中上游记录（★ 预注）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
