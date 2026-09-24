"""审查材料导出 + 原子提交。

分流模型：X = 上游逐字镜像（index-X@pin 的 EPUB/ 树），Y = x2y(X) 净化产物（EPUB/）。

审查材料 = Y 侧 HEAD→工作区的文本块 diff。按构造其中只有上游驱动改动：
校对提交的 rules/ 与 Y 侧规则效果同 commit 入仓（HEAD 自洽），轮末
check_y_freshness 门挡陈旧/直接编辑的 Y。属性/版式/图片改动不产文本块
diff，天然不进审查；opf 时间戳块机械过滤。可疑块在审查期写成 x2y 规则
（校正或回钉），重跑 x2y.py 后 Y 即净化——本脚本不做分类，审查即门控。

--commit（run_all --finish 调用，前置 check_y_freshness）：
- 非文本改动（css/图片/字体等）经两侧逐字节镜像校验后自动随原子提交入仓
  （X 侧随 pin，Y 侧随暂存）；校验不过列出并中止。
- 挂起清单 .triage/hold.txt 内的 rel 跳过 Y 侧提交（X 侧随 pin 入仓）。
- 一笔原子提交 {gitlink 推进, 全部 Y 侧改动}；提交后工作区必须清零
  （仅剩挂起时单列报告并以非零码退出，轮次保持开放）。
用法: uv run python scripts/sync/triage_text.py [--commit]
"""

import difflib
import os
import re
import shutil
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import (
    STATE_DIR,
    TEXT_EXT,
    X_PREFIX,
    X_REPO,
    CatFile,
    clear_sync_ref,
    git,
    head_sha_map,
    hold_path,
    norm_ws,
    pin_sha,
    prune_hold,
    read_hold,
    sync_ref,
    text_chunks,
    triage_parser,
    x_head,
)

# .triage 中脚本自管的状态文件；其余（verdicts、review_chunks 等）是审查草稿
MANAGED_STATE = {
    "hold.txt",
    "review_changes.txt",
    "upstream_context.txt",
    "sync_ref",
}

# 审查草稿目录（prepare_review 的产物），随新一轮导出整体轮转
REVIEW_DIRS = ("verdicts", "review_chunks", "review_prompts")

# opf 的 dcterms:modified 时间戳块：机械更新，不进审查
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def clean_scratch() -> None:
    """默认模式导出新材料 = 新一轮审查开始：把上轮审查草稿（散文件 +
    REVIEW_DIRS 目录）轮转入 prev/（仅保留一轮），防止旧 verdict 被误当
    本轮结论。--commit 模式不清：中止时审查仍在继续，草稿还有用。"""
    scratch = [
        f
        for f in os.listdir(STATE_DIR)
        if f not in MANAGED_STATE
        and f != "prev"
        and os.path.isfile(os.path.join(STATE_DIR, f))
    ]
    dirs = [d for d in REVIEW_DIRS if os.path.isdir(os.path.join(STATE_DIR, d))]
    if not scratch and not dirs:
        return
    prev = os.path.join(STATE_DIR, "prev")
    shutil.rmtree(prev, ignore_errors=True)
    os.makedirs(prev)
    for f in scratch + list(dirs):
        os.replace(os.path.join(STATE_DIR, f), os.path.join(prev, f))
    print(f"上轮审查草稿 {len(scratch) + len(dirs)} 个移入 {prev}")


def term_summary(o: str, n: str, maxlen: int = 40) -> str:
    sm = difflib.SequenceMatcher(a=o, b=n, autojunk=False)
    outs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        outs.append(f"{o[i1:i2][:maxlen]}→{n[j1:j2][:maxlen]}")
    return "；".join(outs)


def block_changes(old: bytes, new: bytes) -> list[tuple[str, str]]:
    """Y 文本块 diff（norm_ws 形态）：1:1 替换为逐块 (旧, 新)；增删与
    非等长替换（结构改动）以块组呈现（一侧为空串表示纯增/删）。
    opf 时间戳块已滤除。"""
    oc = [norm_ws(c) for c in text_chunks(old)]
    nc = [norm_ws(c) for c in text_chunks(new)]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=oc, b=nc, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            out.extend((oc[i], nc[j]) for i, j in zip(range(i1, i2), range(j1, j2)))
        else:
            out.append(("\n".join(oc[i1:i2]), "\n".join(nc[j1:j2])))
    return [(o, n) for o, n in out if not (_TS.fullmatch(o) and _TS.fullmatch(n))]


def y_status() -> list[tuple[str, str, str | None]]:
    """Y 侧在途改动（暂存口径）：[(状态, rel, 旧rel)]；状态 M/A/D/R。"""
    git("add", "-A")
    entries = git("status", "--porcelain", "-z").decode("utf-8").split("\0")
    out = []
    i = 0
    while i < len(entries):
        e = entries[i]
        if not e:
            i += 1
            continue
        st, p = e[0], e[3:]
        if st == "R":  # status -z 顺序为 to\0from
            if p.startswith("EPUB/"):
                out.append(("R", p[5:], entries[i + 1]))
            i += 2
        else:
            if p.startswith("EPUB/"):
                out.append((st, p[5:], None))
            i += 1
    return out


def collect() -> dict:
    """枚举 Y 侧在途改动并提取文本块 diff。

    返回 {blocks, new_files, deleted, nontext, format_only}：
    - blocks: rel -> [(旧块, 新块)]（M/R 文本文件；rename 跨路径取旧 blob）
    - new_files/deleted: 新增/删除的文本文件 rel（随原子提交，不入块审查）
    - format_only: 文本块零差异的文本文件（纯格式化/属性/版式改动，
      审查天然不可见，随原子提交）
    - nontext: 非文本改动 rel（--commit 时经镜像校验自动入仓）
    """
    yhm = head_sha_map()
    cf = CatFile()
    blocks: dict[str, list[tuple[str, str]]] = {}
    new_files, deleted, nontext, format_only = [], [], [], []
    try:
        for st, rel, old_rel in y_status():
            if not rel.lower().endswith(TEXT_EXT):
                nontext.append(rel)
                continue
            if st == "D":
                deleted.append(rel)
                continue
            if st == "A" or "EPUB/" + rel not in yhm:
                new_files.append(rel)
                continue
            old = cf.read(yhm["EPUB/" + (old_rel or rel)])
            with open(os.path.join("EPUB", rel), "rb") as f:
                new = f.read()
            ch = block_changes(old, new)
            if ch:
                blocks[rel] = ch
            else:
                format_only.append(rel)
    finally:
        cf.close()
    return {
        "blocks": blocks,
        "new_files": new_files,
        "deleted": deleted,
        "nontext": nontext,
        "format_only": format_only,
    }


def mirrored(rel: str) -> bool:
    """非文本改动两侧镜像一致（字节相同或两侧同缺）。"""
    xp, yp = os.path.join(X_REPO, X_PREFIX, rel), os.path.join("EPUB", rel)
    xe, ye = os.path.exists(xp), os.path.exists(yp)
    if not xe and not ye:
        return True
    if xe != ye:
        return False
    with open(xp, "rb") as f:
        xb = f.read()
    with open(yp, "rb") as f:
        yb = f.read()
    return xb == yb


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="覆盖核验后一笔原子提交全部改动；默认只导出审查材料",
    )
    do_commit = parser.parse_args().commit

    # 挂起清单：失效条目（Y 侧无在途改动）每轮自动剔除
    for _rel in prune_hold():
        print(f"挂起条目失效（Y 侧无在途改动），剔除: {_rel}")
    hold = read_hold()

    r = collect()
    blocks = r["blocks"]
    new_files, deleted = r["new_files"], r["deleted"]
    nontext, format_only = r["nontext"], r["format_only"]
    review_blocks = {rel: ch for rel, ch in blocks.items() if rel not in hold}
    n_blocks = sum(len(ch) for ch in review_blocks.values())
    print(f"{len(review_blocks):5d}  有文本改动的文件（待审查）")
    print(f"{n_blocks:5d}  改动块（去重前）")
    if new_files:
        print(f"{len(new_files):5d}  新增文件（随原子提交）")
    if deleted:
        print(f"{len(deleted):5d}  删除文件（随原子提交）")
    if format_only:
        print(f"{len(format_only):5d}  纯格式化/属性改动（文本不可见，随原子提交）")
    if nontext:
        print(f"{len(nontext):5d}  非文本改动（镜像校验后随原子提交）")

    # 导出审查材料：按块聚合（相同改动跨文件去重为 [N次]）
    os.makedirs(STATE_DIR, exist_ok=True)
    agg = Counter()
    agg_rels = defaultdict(list)
    for rel, ch in review_blocks.items():
        for o, n in ch:
            agg[(o, n)] += 1
            agg_rels[(o, n)].append(rel)
    with open(
        os.path.join(STATE_DIR, "review_changes.txt"), "w", encoding="utf-8"
    ) as f:
        f.writelines(
            f"[{c}次] {'、'.join(agg_rels[(o, n)])}\n- {o}\n+ {n}\n\n"
            for (o, n), c in agg.most_common()
        )
    print(
        f"审查材料: {os.path.join(STATE_DIR, 'review_changes.txt')}"
        f"（{sum(agg.values())} 块 / 去重 {len(agg)}）"
    )

    if not do_commit:
        if hold:
            print(f"挂起清单 {len(hold)} 条（{hold_path()}）")
        clean_scratch()
        print(
            "审查 review_changes.txt；可疑改动写 x2y 规则（校正或回钉旧文本），"
            "重跑 uv run python scripts/sync/x2y.py，再带 --commit 运行"
        )
        return

    # 覆盖核验（先于提交，中止时本轮不产生任何提交）：Y 侧每条在途改动
    # 都必须入覆盖集（文本块/新增/删除/纯格式化/非文本镜像/挂起），否则
    # 列出并中止；非 Y 侧改动（rules/、scripts/ 等 stray）同样列出；
    # gitlink（index-X）是本轮正常组成。
    covered = set(blocks) | set(new_files) | set(deleted) | set(format_only)
    uncovered, y_auto = [], set()
    for st, rel, _old in y_status():
        if rel in hold or rel in covered:
            continue
        if not rel.lower().endswith(TEXT_EXT):
            if mirrored(rel):
                y_auto.add(rel)  # 非文本镜像改动：自动入仓
            else:
                uncovered.append(f"EPUB/{rel}（非文本，两侧不一致）")
        else:
            uncovered.append(f"EPUB/{rel}（未入覆盖集）")
    for line in git("status", "--porcelain").decode("utf-8").splitlines():
        if not line or line[3:].strip('"') == X_REPO:
            continue
        p = line[3:].strip('"')
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        if p.startswith("EPUB/"):
            continue  # Y 侧已逐条覆盖核验
        uncovered.append(line)
    if uncovered:
        print(
            "中止：以下改动未被覆盖（stray 文件/非镜像二进制等），"
            "人工审查按性质提交后重跑 --commit："
        )
        print("\n".join(uncovered[:30]))
        raise SystemExit(1)

    # 原子提交 {gitlink 推进, 全部 Y 侧改动}（跳过挂起清单内 rel 的 Y 侧；
    # X 侧随 pin 全量入仓）。Y == x2y(X) 由 check_y_freshness 保证
    # （run_all --finish 前置）。
    commit_rels = sorted((covered | y_auto) - set(hold))
    need_bump = x_head() != pin_sha()
    n_commit_blocks = sum(
        len(blocks.get(rel, []))
        for rel in commit_rels
        if rel not in set(new_files) | set(deleted)
    )
    if commit_rels or need_bump:
        ref = sync_ref()
        if not commit_rels:
            subject = f"fix: sync X ← index-X@{ref}"
        elif n_commit_blocks == 0:
            subject = f"fix: sync X/Y ← index-X@{ref}（{len(commit_rels)} 文件）"
        else:
            subject = (
                f"fix: sync X/Y ← index-X@{ref}"
                f"（{len(commit_rels)} 文件，{n_commit_blocks} 处文本修订）"
            )
        body_lines = []
        new_set = set(new_files)
        for rel in commit_rels:
            if rel in new_set:
                body_lines.append(f"{rel}（新增文件）")
                continue
            ch = blocks.get(rel)
            if not ch:
                continue
            terms = [t for t in (term_summary(o, n) for o, n in ch) if t]
            body_lines.append(
                f"{rel}（{len(ch)} 处）: "
                + "；".join(terms[:3])
                + ("…" if len(terms) > 3 else "")
            )
        body = "\n".join(body_lines[:40])
        if len(body_lines) > 40:
            body += f"\n…（共 {len(body_lines)} 文件）"
        if y_auto:
            body += (
                f"\n非文本镜像改动 {len(y_auto)} 文件（css/图片等，"
                "两侧逐字节一致，随 pin 入仓）"
            )

        # 暂存口径 = 工作区：reset 清空在途暂存（collect 的 add -A 会把挂起
        # 文件也一并暂存），再精确暂存 gitlink 与 Y 侧待提交路径。
        # rename 旧路径须在 reset 前取（y_status 自带 add -A，会把挂起文件
        # 重新暂存；reset 之后不得再调任何带 add 的助手）
        rename_olds = [
            f"EPUB/{old_rel}"
            for st, rel, old_rel in y_status()
            if st == "R" and rel in commit_rels and old_rel
        ]
        git("reset", "-q")
        paths = [X_REPO] + [f"EPUB/{rel}" for rel in commit_rels] + rename_olds
        git(
            "add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            input_bytes="\0".join(paths).encode("utf-8"),
        )
        args = ["commit", "-q", "-m", subject]
        if body:
            args += ["-m", body]
        git(*args)
        # pin 已推进：同步 ref 状态失效（挂起残留同理，下一轮从新 pin 起算）
        clear_sync_ref()

    git("add", "-A")
    left = [l for l in git("status", "--porcelain").decode("utf-8").splitlines() if l]
    if left:
        # 仅剩挂起改动：单列报告，非零退出（轮次保持开放）
        print(
            f"挂起 {len(left)} 条在途改动（{hold_path()}；原因消除后从清单删行，"
            "重跑 --finish 即可清零）："
        )
        for rel, reason in sorted(hold.items()):
            print(f"  {rel}: {reason}")
        raise SystemExit(1)
    if commit_rels:
        print(
            f"完成。一笔原子提交 {len(commit_rels)} 个文件"
            f"（{n_commit_blocks} 处文本修订），工作区已清零"
        )
    else:
        print("工作区已清零（无待提交改动）")


if __name__ == "__main__":
    main()
