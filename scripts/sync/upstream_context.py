"""上游上下文导出：本轮 pin 区间的 commit 记录 + maintenance-records 命中预注，供审查参考。

参考源是 index-X submodule（全程经 git plumbing 读，不依赖其工作区状态）。
区间精确：基准 = superproject HEAD 的 gitlink pin（上轮同步点），
新侧 = submodule HEAD（本轮 update_x checkout 的 ref）；未起新轮
（pin == HEAD）时静默跳过并清除陈旧产物。
记录是上游自报证据，【不免检】：本脚本只做机械层（区间列举、片段命中检索），
「引文是否真支持改动」的语义判断保留给审查者
（.agents/skills/sync-triage/SKILL.md §6 上游记录的使用纪律）。

产物（.triage/ 下）：
- upstream_context.txt：pin 区间、逐卷新 commit 列表（含 body）、区间内
  新增/修改的 maintenance-records 全文、全部记录索引、政策文件变更。
- review_changes.txt 块尾预注（★ 前缀行，prepare_review 的 parse_blocks 跳过）：
  ① commit 归属——块文本在区间内逐 commit 的 diff 纯文本中整段逐字命中，
  标出引入 commit（短 sha + subject，时间序；2+ 个 = 振荡链/多区域块/拆分
  波次）；② records 命中——块的最小差异片段在全部 records 中检索的命中。
--fetch：先 git fetch（起新轮时 run_all 带入）；在途重跑不带，离线幂等。
用法: uv run python scripts/sync/upstream_context.py [--fetch]
"""

import difflib
import os
import re
import sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import (
    STATE_DIR,
    X_PREFIX,
    git,
    gitx,
    norm_ws,
    pin_sha,
    status_line_rel,
    text_chunks,
    triage_parser,
    x_changes,
    x_head,
)

RECORDS_DIR = "docs/maintenance-records"
MAX_HIT_RECS = 4  # 每块最多列出的命中记录数
MAX_HIT_LINES = 2  # 每条记录最多列出的命中行数
MAX_LINE = 160  # 命中行截断
ATTR_LONG = 10  # 较长侧 ≥ATTR_LONG 为长块：单侧整段命中即归属（整句逐字巧合可忽略）
ATTR_MIN_SIDE = 2  # 任一侧 <ATTR_MIN_SIDE（1 字）无区分度，未定位
ATTR_MIN_SINGLE = 6  # 纯增删块（一侧为空）：非空侧 ≥ATTR_MIN_SINGLE 才归属
MAX_HIT_COMMITS = 4  # 每块最多列出的归属 commit 数
BLOCK_HEAD = re.compile(r"^(\[\d+次]|\[[XY]\]) ")


def vol_commits(base: str, head: str, vol: str) -> list[str]:
    """base..head 中改动过 EPUB/<vol> 的 commit（新→旧）。"""
    out = gitx("log", "--format=%H", f"{base}..{head}", "--", f"{X_PREFIX}/{vol}")
    return [l for l in out.decode("utf-8").splitlines() if l]


def commit_brief(sha: str) -> tuple[str, str, str]:
    """(短 sha, 日期, subject+body)。body 原样保留（上游的差异摘要）。"""
    out = gitx("show", "-s", "--format=%h %cI%n%B", sha).decode("utf-8")
    first, _, body = out.partition("\n")
    short, _, date = first.partition(" ")
    return short, date, body.rstrip("\n")


def changed_vols() -> list[str]:
    """本轮在途改动涉及的卷（X 侧 pin..HEAD ∪ Y 侧工作区）。"""
    vols = set()
    for _st, rel, old in x_changes():
        vols.add(rel.split("/")[0])
        if old:
            vols.add(old.split("/")[0])
    for line in git("status", "--porcelain").decode("utf-8").splitlines():
        rel = status_line_rel(line)
        if rel is not None:
            vols.add(rel.split("/")[0])
    return sorted(vols)


def records_index(head: str) -> list[str]:
    out = gitx("ls-tree", "-r", "--name-only", head, "--", RECORDS_DIR).decode("utf-8")
    return [l for l in out.splitlines() if l.endswith(".md")]


def record_text(head: str, path: str) -> str:
    return gitx("show", f"{head}:{path}").decode("utf-8")


def changed_records(base: str, head: str) -> list[str]:
    """pin 区间内新增/修改的 record 路径。"""
    out = gitx(
        "log", "--format=", "--name-status", f"{base}..{head}", "--", RECORDS_DIR
    ).decode("utf-8")
    seen: list[str] = []
    for l in out.splitlines():
        if l and l[0] in "AM" and "\t" in l:
            p = l.split("\t", 1)[1]
            if p not in seen:
                seen.append(p)
    return seen


def policy_changes(base: str, head: str) -> list[str]:
    out = gitx(
        "log",
        "--format=",
        "--name-only",
        f"{base}..{head}",
        "--",
        "AGENTS.md",
        ".agents",
    ).decode("utf-8")
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


class CommitDiff(NamedTuple):
    """区间内一个触及 EPUB/ 的 commit：短 sha、subject、删除/新增侧纯文本。

    纯文本经 text_chunks 去标记 + norm_ws，与审查块文本同形态（防标签边界、
    实体、空白差异造成的假阴性）。
    """

    short: str
    subject: str
    removed: str
    added: str


def commit_corpus(base: str, head: str) -> list[CommitDiff]:
    """pin 区间内触及 EPUB/ 的 commit（旧→新时间序，振荡链按叙述顺序呈现）。"""
    shas = (
        gitx("log", "--format=%H", f"{base}..{head}", "--", X_PREFIX).decode().split()
    )
    out = []
    for sha in reversed(shas):  # log 输出新→旧，反转回时间序
        subject = gitx("show", "-s", "--format=%s", sha).decode().strip()
        diff = gitx("show", "--format=", "-U0", sha, "--", X_PREFIX).decode()
        removed = [l[1:] for l in diff.splitlines() if l[:1] == "-" and l[:3] != "---"]
        added = [l[1:] for l in diff.splitlines() if l[:1] == "+" and l[:3] != "+++"]

        def plain(lines: list[str]) -> str:
            runs = text_chunks("\n".join(lines).encode("utf-8", "replace"))
            return "\n".join(t for t in (norm_ws(r) for r in runs) if t)

        out.append(CommitDiff(sha[:8], subject, plain(removed), plain(added)))
    return out


def attribute(old: str, new: str, corpus: list[CommitDiff]) -> list[CommitDiff]:
    """块的归属 commit（时间序）：块文本在 commit diff 纯文本中的整段逐字命中。

    整段逐字（而非碎片统计）天然免疫巧合：完整句子在同区间无关 commit 的
    diff 中逐字出现的概率可忽略，无需防巧合门槛。多 commit 归属均属合法：
    振荡链（旧全文命中首改的 removed、新全文命中末改的 added）、多区域段落
    块（各 commit 各改一处）、聚合块的拆分波次。长度校准：短块（较长侧
    <ATTR_LONG）要求双侧命中同一 commit（单侧短词巧合多）；任一侧
    <ATTR_MIN_SIDE（1 字）无区分度；纯增删块非空侧 ≥ATTR_MIN_SINGLE 才归属。
    漏归（ruby 隔断、规则交叠等块文本与 X 侧不逐字一致的情形）以未定位呈现
    ——宁可空缺不错指。
    """
    if not old or not new:
        t = old or new
        if len(t) < ATTR_MIN_SINGLE:
            return []
        return [c for c in corpus if t in (c.removed if old else c.added)]
    if min(len(old), len(new)) < ATTR_MIN_SIDE:
        return []
    need_both = max(len(old), len(new)) < ATTR_LONG
    out = []
    for c in corpus:
        o_hit, n_hit = old in c.removed, new in c.added
        if (o_hit and n_hit) if need_both else (o_hit or n_hit):
            out.append(c)
    return out


def search_records(corpus: dict[str, str], frags: set[str]) -> list[str]:
    """片段在全部 records 中的命中行（★ 预注行列表，限量截断）。"""
    hits, n_rec = [], 0
    for path, text in corpus.items():
        if n_rec >= MAX_HIT_RECS:
            break
        matched = [
            l.strip()[:MAX_LINE]
            for l in text.splitlines()
            if any(f in l for f in frags)
        ]
        if matched:
            n_rec += 1
            hits += [
                f"★ {os.path.basename(path)}: {l}" for l in matched[:MAX_HIT_LINES]
            ]
    return hits


def annotate(
    path: str, corpus: dict[str, str], commits: list[CommitDiff]
) -> tuple[int, int]:
    """在 review/suspect 材料每块尾部插入 ★ 预注行；返回 (commit 归属块数, records 命中块数)。

    行级遍历保持原格式：块 = [N次]/[X|Y] 头行 + -/+ 行与续行，空行收尾。
    triage_text 每次导出都覆写原材料，故预注天然幂等（材料更新后重跑本脚本）。
    块尾先列 commit 归属（块的引入 commit；2+ 个 = 振荡链/多区域块/拆分波次），
    再列 records 命中。
    """
    if not os.path.exists(path):
        return 0, 0
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out: list[str] = []
    n_commit, n_records = 0, 0
    old: list[str] = []
    new: list[str] = []
    side: list[str] | None = None

    def flush() -> None:
        nonlocal n_commit, n_records
        o_text, n_text = "\n".join(old), "\n".join(new)
        if not o_text and not n_text:
            return  # 文件末尾的空 flush（或零区域块）：无可归属，不产预注行
        if commits:
            at = attribute(o_text, n_text, commits)
            if at:
                n_commit += 1
                for c in at[:MAX_HIT_COMMITS]:
                    out.append(f"★ commit {c.short}: {c.subject}")
                if len(at) > MAX_HIT_COMMITS:
                    out.append(f"★ commit …共 {len(at)} 个，详见 upstream_context.txt")
            else:
                out.append("★ commit: 未定位")
        hits = search_records(corpus, windows_of(o_text, n_text))
        out.extend(hits)
        n_records += bool(hits)
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
    return n_commit, n_records


def main() -> int:
    parser = triage_parser(__doc__)
    parser.add_argument(
        "--fetch", action="store_true", help="先 git fetch index-X（起新轮时用）"
    )
    args = parser.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    ctx_path = os.path.join(STATE_DIR, "upstream_context.txt")

    if args.fetch:
        print("fetch index-X ...", flush=True)
        gitx("fetch", "-q", "--tags", "--force", "origin")
    base, head = pin_sha(), x_head()
    if base == head:
        if os.path.exists(ctx_path):
            os.remove(ctx_path)  # 防陈旧文件被误当本轮材料
        print("pin 未推进（未起新轮），跳过上游上下文导出")
        return 0

    vols = changed_vols()
    commits = {
        v: [(s, commit_brief(s)) for s in vol_commits(base, head, v)] for v in vols
    }
    recs = records_index(head)
    corpus = {p: record_text(head, p) for p in recs}
    new_recs = changed_records(base, head)
    policies = policy_changes(base, head)
    master = gitx("rev-parse", "origin/master").decode().strip()
    ahead = gitx("rev-list", "--count", f"{head}..{master}").decode().strip()

    tags = gitx("tag", "--points-at", head).decode().split()
    w = [
        f"# 上游上下文（index-X pin 区间 {base[:8]}..{head[:8]}"
        + (f"，tag {max(tags)}" if tags else "")
        + "）",
        "# 记录是上游自报证据，不免检：机械比对已做，",
        "# 「引文→改动」的语义支持关系仍需审查者逐条判断。",
    ]
    if ahead != "0":
        w.append(f"# 注：origin/master 领先本轮同步点 {ahead} 个 commit（属下轮内容）")
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
    w += ["", f"## maintenance-records 变更（区间内 {len(new_recs)} 篇）"]
    for p in new_recs:
        w += [f"===== {p} =====", corpus.get(p, ""), ""]
    w += ["", "## 全部记录索引（块级 ★ 预注的检索范围）"]
    w += [f"- {p}" for p in recs]
    if policies:
        w += ["", "## 政策文件变更（AGENTS.md/.agents，区间内）"]
        w += [f"- {p}" for p in policies]
    with open(ctx_path, "w", encoding="utf-8") as f:
        f.write("\n".join(w) + "\n")
    print(
        f"上游上下文: {ctx_path}"
        f"（{len(vols)} 卷，新 commit {sum(len(v) for v in commits.values())} 条，"
        f"records 变更 {len(new_recs)} 篇）"
    )

    n_commit, n_rec = annotate(
        os.path.join(STATE_DIR, "review_changes.txt"), corpus, commit_corpus(base, head)
    )
    print(
        f"review_changes.txt: {n_commit} 块归属上游 commit，"
        f"{n_rec} 块命中上游记录（★ 预注）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
