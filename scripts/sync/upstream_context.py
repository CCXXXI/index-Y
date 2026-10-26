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
- review_changes.json：块级机器权威预注（commits 为 8 位 sha；annotations
  为人类可读预注）；review_changes.txt 由该 JSON 重生成，仅供人读：
  ① commit 归属——块文本在区间内逐 commit 的 diff 纯文本中整段逐字命中，
  标出引入 commit（短 sha + subject，时间序；2+ 个 = 振荡链/多区域块/拆分
  波次）；② records 命中——块的最小差异片段在全部 records 中检索的命中。
--fetch：先 git fetch（起新轮时 run_all 带入）；在途重跑不带，离线幂等。
用法: uv run python scripts/sync/upstream_context.py [--fetch]
"""

import difflib
import os
import sys
from functools import cache
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
from triage_text import read_review_changes, write_review_changes

RECORDS_DIR = "docs/maintenance-records"
MAX_HIT_RECS = 4  # 每块最多列出的命中记录数
MAX_HIT_LINES = 2  # 每条记录最多列出的命中行数
MAX_LINE = 160  # 命中行截断
ATTR_LONG = 10  # 较长侧 ≥ATTR_LONG 为长块：单侧整段命中即归属（整句逐字巧合可忽略）
ATTR_MIN_SIDE = 2  # 任一侧 <ATTR_MIN_SIDE（1 字）无区分度，未定位
ATTR_MIN_SINGLE = 6  # 纯增删块（一侧为空）：非空侧 ≥ATTR_MIN_SINGLE 才归属
MAX_HIT_COMMITS = 4  # 每块最多列出的归属 commit 数


def vol_commits(base: str, head: str, vol: str) -> list[str]:
    """base..head 中改动过 EPUB/<vol> 的 commit（新→旧）。"""
    out = gitx("log", "--format=%H", f"{base}..{head}", "--", f"{X_PREFIX}/{vol}")
    return [l for l in out.decode("utf-8").splitlines() if l]


@cache  # 同一 commit 触及多卷时在逐卷列表中重复出现
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


class RecordsIndex:
    """全部 records 的行级 trigram 倒排索引：frag → 命中行 id（语料序，逐字验证）。

    search_records 每块平均数百个 frag，逐行扫全语料是 O(块×frag×行) 的三重扫描。
    反转为「frag 定位行」：line ⊇ frag ⇒ line 含 frag 的全部 trigram，取最稀有
    trigram 的倒排表做候选，再 `frag in line` 逐字验证（充分条件过滤，零误纳
    零漏纳）。行 id 按（记录序, 行内序）分配，候选结果与原逐行扫描逐字节一致。
    """

    def __init__(self, corpus: dict[str, str]) -> None:
        self.paths = list(corpus)
        self.lines: list[str] = []
        self.rec_of: list[int] = []
        for i, p in enumerate(self.paths):
            for line in corpus[p].splitlines():
                self.rec_of.append(i)
                self.lines.append(line)
        postings: dict[str, list[int]] = {}
        for lid, line in enumerate(self.lines):
            for i in range(len(line) - 2):  # 短于 3 的行容不下任何 frag（键长 ≥3）
                postings.setdefault(line[i : i + 3], []).append(lid)
        self.postings = postings
        self._cache: dict[str, tuple[int, ...]] = {}  # frag 跨块大量重复

    def frag_lines(self, frag: str) -> tuple[int, ...]:
        hit = self._cache.get(frag)
        if hit is not None:
            return hit
        if len(frag) < 3:  # windows_of 不产出（键长 ≥3），防御性全扫保语义
            hit = tuple(lid for lid, t in enumerate(self.lines) if frag in t)
        else:
            grams = (frag[i : i + 3] for i in range(len(frag) - 2))
            best = min(grams, key=lambda g: len(self.postings.get(g, ())))
            hit = tuple(
                lid for lid in self.postings.get(best, ()) if frag in self.lines[lid]
            )
        if len(self._cache) < 500_000:  # 上限防长轮次内存膨胀
            self._cache[frag] = hit
        return hit


def search_records(idx: RecordsIndex, frags: set[str]) -> list[str]:
    """片段在全部 records 中的命中行（★ 预注行列表，限量截断）。

    行 id 随（记录序, 行内序）单调，排序遍历与原「逐记录逐行扫描、命中
    MAX_HIT_RECS 条记录即停」的输出逐字节一致。
    """
    hits, per_rec, prev = [], {}, -1
    for lid in sorted({l for f in frags for l in idx.frag_lines(f)}):
        r = idx.rec_of[lid]
        if r != prev:
            if len(per_rec) >= MAX_HIT_RECS:
                break
            prev = r
        if per_rec.get(r, 0) >= MAX_HIT_LINES:
            continue
        per_rec[r] = per_rec.get(r, 0) + 1
        hits.append(
            f"★ {os.path.basename(idx.paths[r])}: {idx.lines[lid].strip()[:MAX_LINE]}"
        )
    return hits


def annotate(
    path: str, corpus: dict[str, str], commits: list[CommitDiff]
) -> tuple[int, int]:
    """把 commit 归属与 records 预注写回 JSON，并由 JSON 重生成 TXT。

    commits 保留全部 8 位归属 sha；annotations 保存 TXT 去掉 ``★ `` 的
    全部预注行（含人类可读的 commit subject、未定位与记录命中）。
    """
    triage = os.path.dirname(path)
    if not os.path.exists(os.path.join(triage, "review_changes.json")):
        if not os.path.exists(path):
            return 0, 0
        read_review_changes(triage)  # 旧 TXT：显式报迁移错误
    blocks = read_review_changes(triage)
    idx = RecordsIndex(corpus)
    n_commit, n_records = 0, 0
    for b in blocks:
        o_text, n_text = b["old"], b["new"]
        b["commits"] = []
        b["annotations"] = []
        if not o_text and not n_text:
            continue
        if commits:
            at = attribute(o_text, n_text, commits)
            if at:
                n_commit += 1
                b["commits"] = [c.short for c in at]
                for c in at[:MAX_HIT_COMMITS]:
                    b["annotations"].append(f"commit {c.short}: {c.subject}")
                if len(at) > MAX_HIT_COMMITS:
                    b["annotations"].append(
                        f"commit …共 {len(at)} 个，详见 upstream_context.txt"
                    )
            else:
                b["annotations"].append("commit: 未定位")
        hits = search_records(idx, windows_of(o_text, n_text))
        b["annotations"].extend(h.removeprefix("★ ") for h in hits)
        n_records += bool(hits)
    write_review_changes(triage, blocks)
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
    try:
        merge_base = gitx("merge-base", base, head).decode().strip()
    except RuntimeError:
        merge_base = ""
    if merge_base != base:
        print(
            "错误：仅支持 pin 为新 HEAD 祖先的正向区间；"
            f"拒绝反向/分叉区间 {base[:8]}..{head[:8]}"
        )
        return 2

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
