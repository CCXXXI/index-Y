"""xhtml 重命名 → 一律纯移动提交（commit 只移动不改内容），捆绑改动各回各的管道。

上游重编号章节文件时常捆绑少量修订（相似度 98~99%）。本脚本对所有文本 rename
做「只移动不改内容」的纯移动提交（HEAD blob 写新路径，全部 R100 程序化验证；
纯移动不放行任何未核实内容，新路径 = HEAD 字节），捆绑改动随文件成为 M 态后
按性质分流（与非 rename 改动同管道）：

- 标点/空白级或无改动 → 残留随 triage_text 正常分流；
- 属性改动（class/href 等，文本不变）→ 版式批自动吸收（本脚本在版式批之前运行）；
- 结构改动（文本块增删）→ triage_text 结构文件通道（收敛随原子提交/不收敛门控中止）；
- 语义片段 → 写入挂起清单 .triage/hold.txt（M 态挂起，triage_text --commit
  跳过且不计入门控），待人工对照原文核实后从清单删行，随 --finish 原子提交。

用法: uv run python scripts/sync/commit_xhtml_renames.py [--dry-run]
"""

import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commit_image_renames import STATE_DIR
from lib_triage import (
    TEXT_EXT,
    CatFile,
    chunk_changes,
    frag_set,
    git,
    head_sha_map,
    hold_path,
    parse_events,
    staged_renames,
    triage_parser,
)

_CONTENT = re.compile(r"[一-鿿A-Za-z0-9]")


def repair(pairs: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str]]:
    """按（侧, 目录, 去数字/下划线的文件名）确定性重配 rename 对。

    git 按内容相似度配对，X/Y 两侧内容近似时可能跨侧错配（会把 X 的 HEAD
    blob 写到 Y 的新路径，污染 Y = x2y(X) 的纯移动前提）。重配不改变索引
    内容（每对删旧路径、新路径写旧 blob），只纠正归属。
    返回 (配对, 歧义路径)；歧义（同名键多对多）不纯移动，整对挂起待人工。
    """

    def key(p: str) -> tuple[str, str, str]:
        side, rest = p.split("/", 1)
        d, b = rest.rsplit("/", 1)
        return side, d, re.sub(r"[\d_]+", "", b)

    froms: dict[tuple, list] = defaultdict(list)
    tos: dict[tuple, list] = defaultdict(list)
    for f, t in pairs:
        froms[key(f)].append(f)
        tos[key(t)].append(t)
    out, ambiguous = [], []
    for k in sorted(set(froms) | set(tos)):
        fs, ts = froms.get(k, []), tos.get(k, [])
        if len(fs) == 1 and len(ts) == 1:
            out.append((fs[0], ts[0]))
        else:
            ambiguous += fs + ts
    return out, sorted(ambiguous)


def is_semantic(frag_old: str, frag_new: str) -> bool:
    """片段是否语义级：剔除非内容字符（标点/空白/符号）后仍不同。"""
    return "".join(_CONTENT.findall(frag_old)) != "".join(_CONTENT.findall(frag_new))


def non_text_equal(old: bytes, new: bytes) -> bool:
    """两侧除文本载荷外一致（属性多重集合 + 事件序列，文本事件占位化）。"""
    os_, oa = parse_events(old)
    ns, na = parse_events(new)
    if oa != na:
        return False

    def norm(seq):
        return ["TEXT" if e[0] == "text" else e for e in seq]

    return norm(os_) == norm(ns)


def classify_pair(head: bytes, work: bytes) -> tuple[str, list[tuple[str, str]]]:
    """(判定, 改动块列表)。判定仅用于挂起决策，不影响是否纯移动。"""
    if head == work:
        return "clean", []
    edits = chunk_changes(head, work)
    if edits is None:
        return "结构改动", []
    if not non_text_equal(head, work):
        return "属性改动", []
    if any(is_semantic(a, b) for o, n in edits for a, b in frag_set([(o, n)])):
        return "语义片段", edits
    return "clean", edits


def update_hold(hold: dict[str, list], current_rels: set[str]) -> str:
    """rename 挂起并入挂起清单。

    本轮 rename 集合内 rel 的旧 rename: 条目重写（反映本轮判定）；其余条目
    （人工条目、已纯移动的语义挂起——其 rel 不再出现于 rename 集合）原样保留，
    失效条目由 triage_text 每轮复验剔除。人工条目与本轮判定冲突时以人工条目为准。
    """
    path = hold_path()
    manual = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                rel, _, reason = line.partition("\t")
                if reason.strip().startswith("rename:") and rel.strip() in current_rels:
                    continue  # 本轮 rename 集合内的旧条目由下方新条目取代
                manual.append(line)
    manual_rels = {l.partition("\t")[0].strip() for l in manual}
    lines = [
        (
            "# 挂起清单：triage_text --commit 跳过其中 rel 的提交且不计入门控，"
            "末尾单列报告；失效条目（对应文件无在途改动）每轮自动剔除。"
        ),
        (
            "# rename: 前缀条目由 commit_xhtml_renames 生成"
            "（其下 # 注释为改动摘要），其余为人工条目（rel<TAB>理由）。"
        ),
    ]
    lines += manual
    for rel in sorted(hold):
        if rel in manual_rels:
            continue  # 人工条目优先
        verdicts = sorted({v for _, v, _ in hold[rel]})
        lines.append(f"{rel}\trename:{'、'.join(verdicts)}")
        for _, _, edits in hold[rel]:
            lines.extend(f"#   - {o[:80]}\n#   + {n[:80]}" for o, n in edits[:6])
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    return path


def commit_pure_moves(moves: list[tuple[str, str]]) -> None:
    """纯 rename 提交：新路径写 HEAD blob，全部 R100 校验后提交。"""
    hm = head_sha_map()
    git("reset", "-q")  # 索引回到 HEAD；旧路径需显式零模式行删除（否则无 rename 对）
    zero = "0" * 40
    info = "".join(f"0 {zero}\t{f}\0100644 {hm[f]}\t{t}\0" for f, t in moves)
    git("update-index", "-z", "--index-info", input_bytes=info.encode("utf-8"))
    # 校验暂存区：rename 的旧/新路径集合与 moves 一致且全部 R100。
    # （git 的展示配对在双侧内容相同时可能跨侧，无关紧要——索引内容不变）
    out = git(
        "diff", "--cached", "--name-status", "-z", "-M", "--diff-filter=R"
    ).decode("utf-8")
    parts = [p for p in out.split("\0") if p]
    got_from, got_to = set(), set()
    i = 0
    while i < len(parts):
        assert parts[i] == "R100", f"非纯移动混入: {parts[i + 1]}"
        got_from.add(parts[i + 1])
        got_to.add(parts[i + 2])  # diff -z 为 from, to 顺序
        i += 3
    assert got_from == {f for f, _ in moves} and got_to == {t for _, t in moves}, (
        f"暂存 rename 集合不符: 多 {sorted(got_to - {t for _, t in moves})} "
        f"少 {sorted({t for _, t in moves} - got_to)}"
    )
    git(
        "commit", "-q", "-m", f"refactor: rename xhtml files（{len(moves)} 对，纯移动）"
    )
    git("add", "-A")


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="只分类并写挂起清单，不实际提交"
    )
    dry = parser.parse_args().dry_run
    git("add", "-A")  # 统一从「全部暂存」状态出发
    pairs, ambiguous = repair(
        [(f, t) for f, t in staged_renames() if t.lower().endswith(TEXT_EXT)]
    )
    print(
        f"文本 rename 总数 {len(pairs)}"
        + (f"，配对歧义 {len(ambiguous)}" if ambiguous else "")
    )

    hm = head_sha_map()
    cf = CatFile()
    by_rel: dict[str, list] = defaultdict(list)  # rel -> [(f, t, verdict, edits)]
    for f, t in pairs:
        rel = t.split("/", 1)[1]
        if f not in hm:  # 源路径不在 HEAD（理论不应出现）：无法纯移动，整对挂起
            by_rel[rel].append((f, t, "源不在HEAD", []))
            continue
        head = cf.read(hm[f])
        with open(t, "rb") as fp:
            work = fp.read()
        verdict, edits = classify_pair(head, work)
        by_rel[rel].append((f, t, verdict, edits))
    cf.close()

    moves, hold = [], {}
    for p in ambiguous:  # 配对歧义：不纯移动，整对挂起待人工
        rel = p.split("/", 1)[1] if "/" in p else p
        hold[rel].append((p, "配对歧义", []))
    for rel, es in sorted(by_rel.items()):
        vs = {v for _, _, v, _ in es}
        if "源不在HEAD" in vs:
            hold[rel] = [(f, v, ed) for f, _, v, ed in es]
            continue
        moves += [(f, t) for f, t, _, _ in es]  # 其余一律纯移动
        if "语义片段" in vs:  # 语义改动须核实：文本修订 M 态挂起
            hold[rel] = [(f, v, ed) for f, _, v, ed in es]
    print(f"纯移动 {len(moves)} 对，挂起清单写入 {len(hold)} 对")

    os.makedirs(STATE_DIR, exist_ok=True)
    hp = update_hold(hold, {e[2] for es in by_rel.values() for e in es})
    print(f"挂起清单: {hp}")

    if dry or not moves:
        return
    commit_pure_moves(moves)
    print(f"已提交 {len(moves)} 对纯移动 rename")


if __name__ == "__main__":
    main()
