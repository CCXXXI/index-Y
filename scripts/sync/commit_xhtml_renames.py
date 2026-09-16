"""xhtml 重命名 → 纯移动提交（commit 只移动不改内容）+ 捆绑文本修订挂起清单。

上游重编号章节文件时常捆绑少量文本修订（相似度 98~99%）。本脚本把 rename
拆成「纯移动 + 文本分流」两步（docs/sync-triage.md 配套机械项）：

1. 逐对提取 HEAD→工作区的改动：
   - 干净对（文本无改动，或仅剩标点/空白级片段）→ 纯 rename 提交：HEAD blob
     直接写到新路径（重建索引提交法），全部 R100 程序化验证；残留的标点级
     修订成为 M 态，随 triage_text 正常分流。
   - 挂起对（结构改动/属性改动/含语义片段）→ 整对保持 R 态（rename 也不提），
     写入挂起清单 .triage/hold.txt（rename: 前缀条目每轮重写，人工条目保留），
     待人工对照原文核实后按性质提交。
     rename-only 提交会把文件变成 M，triage_text 会当正常文本对提走，
     故含未核实改动的文件必须整对挂起。
2. 提交后 `git add -A` 恢复其余暂存。

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
    """(判定, 改动块列表)：clean=纯移动可提交；hold=挂起。

    hold 的三种理由：结构改动（文本块增删）、属性/结构差异（triage 不收）、
    含语义片段（需对照原文核实）。
    """
    if head == work:
        return "clean", []
    edits = chunk_changes(head, work)
    if edits is None:
        return "hold:结构改动", []
    if not non_text_equal(head, work):
        return "hold:属性/结构差异", []
    if any(is_semantic(a, b) for o, n in edits for a, b in frag_set([(o, n)])):
        return "hold:语义片段", edits
    return "clean", edits


def update_hold(hold: dict[str, list]) -> str:
    """rename 挂起并入挂起清单：rename: 前缀条目每轮重写，人工条目原样保留。"""
    path = hold_path()
    manual = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.partition("\t")[2].startswith("rename:"):
                    manual.append(line)
    lines = [("# 挂起清单：triage_text --commit 跳过其中 rel 的提交并单列报告；"
              "失效条目（对应文件无在途改动）每轮自动剔除。"),
             ("# rename: 前缀条目由 commit_xhtml_renames 每轮重写"
              "（其下 # 注释为改动摘要），其余为人工条目（rel<TAB>理由）。")]
    lines += manual
    for rel in sorted(hold):
        verdicts = sorted({v.removeprefix("hold:") for _, v, _ in hold[rel]})
        lines.append(f"{rel}\trename:{'、'.join(verdicts)}")
        for _, _, edits in hold[rel]:
            lines.extend(f"#   - {o[:80]}\n#   + {n[:80]}" for o, n in edits[:6])
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    return path


def commit_pure_moves(clean: list[tuple[str, str]]) -> None:
    """干净对做纯 rename 提交：新路径写 HEAD blob，全部 R100 校验后提交。"""
    hm = head_sha_map()
    git("reset", "-q")  # 索引回到 HEAD；旧路径需显式零模式行删除（否则无 rename 对）
    zero = "0" * 40
    info = "".join(f"0 {zero}\t{f}\0" f"100644 {hm[f]}\t{t}\0" for f, t in clean)
    git("update-index", "-z", "--index-info", input_bytes=info.encode("utf-8"))
    # 校验暂存区恰好是这些 rename 且全部 R100（纯移动）
    out = git("diff", "--cached", "--name-status", "-z", "-M",
              "--diff-filter=R").decode("utf-8")
    parts = [p for p in out.split("\0") if p]
    got = []
    i = 0
    while i < len(parts):
        assert parts[i] == "R100", f"非纯移动混入: {parts[i + 1]}"
        got.append((parts[i + 1], parts[i + 2]))  # diff -z 为 from, to 顺序
        i += 3
    assert sorted(got) == sorted(clean), (
        f"暂存 rename 集合不符: 多 {sorted(set(got) - set(clean))} "
        f"少 {sorted(set(clean) - set(got))}")
    git("commit", "-q", "-m", f"refactor: rename xhtml files（{len(clean)} 对，纯移动）")
    git("add", "-A")


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="只分类并写挂起清单，不实际提交")
    dry = parser.parse_args().dry_run
    git("add", "-A")  # 统一从「全部暂存」状态出发
    pairs = [(f, t) for f, t in staged_renames() if t.lower().endswith(TEXT_EXT)]
    print(f"文本 rename 总数 {len(pairs)}")

    hm = head_sha_map()
    cf = CatFile()
    hold: dict[str, list] = defaultdict(list)  # rel -> [(side, reason, edits)]
    clean: list[tuple[str, str]] = []
    for f, t in pairs:
        rel = t.split("/", 1)[1]
        if f not in hm:  # 源路径不在 HEAD（理论不应出现）
            hold[rel].append((f, "hold:源不在HEAD", []))
            continue
        head = cf.read(hm[f])
        with open(t, "rb") as fp:
            work = fp.read()
        verdict, edits = classify_pair(head, work)
        if verdict == "clean":
            clean.append((f, t))
        else:
            hold[rel].append((f, verdict, edits))
    cf.close()

    # X/Y 任一侧挂起则整对挂起（成对提交约束）
    hold_rels = {r for r, items in hold.items()
                 if any(v != "clean" for _, v, _ in items)}
    clean = [(f, t) for f, t in clean if t.split("/", 1)[1] not in hold_rels]
    print(f"干净对 {len(clean)}，挂起 {len(hold_rels)} 对")

    os.makedirs(STATE_DIR, exist_ok=True)
    hp = update_hold(hold)
    print(f"挂起清单: {hp}")

    if dry or not clean:
        return
    commit_pure_moves(clean)
    print(f"已提交 {len(clean)} 对纯移动 rename")


if __name__ == "__main__":
    main()
