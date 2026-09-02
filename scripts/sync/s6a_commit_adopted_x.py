"""批次 6a：提交规则相关的自动分流块（规则采纳 + 规则失效型收敛）。

无需等人工审查，s5 分类后即可运行；s5 --commit 之后应再跑一遍收尾——
此时混合块对的正常同步片段已入 HEAD，重跑分类后其 X 侧剩余 diff 退化为
纯规则采纳块。处理两种 kind：
- "adopted"：仅 X 有改动的规则采纳块，按块提交 X 侧；
- "rulekilled"：旧 X 命中规则、上游改写后两侧逐字收敛的块对，成对提交。
提交粒度为块：apply_blocks(keep=...) 构造中间版本，正常同步候选、
疑似上游错误块自动留在工作区。

收敛判定（convergent）与 rulekilled 的三重条件保证提交后 HEAD 上
x2y(X) == Y 不变式不被破坏。s6b 候选规则导出为累积制的
STATE_DIR/adopted_rules.json（与既有候选合并，s6b 消费后剔除）。
用法: uv run python scripts/sync/s6a_commit_adopted_x.py [--dry-run]
"""

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib_triage import (
    CatFile,
    apply_blocks,
    chunk_changes,
    commit_paths,
    git,
    head_sha_map,
)
from s1_commit_image_renames import STATE_DIR
from s5_triage_text import classify, term_summary


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    r = classify()
    pairs_by_rel, head, work = r["pairs_by_rel"], r["head"], r["work"]
    structural_ok = r.get("structural_ok", set())

    if any(k in ("sync", "mixed")
           for ps in pairs_by_rel.values() for k, _, _ in ps):
        print("提示: 存在正常同步候选/混合块，混合块的规则采纳片段需等 "
              "s5 --commit 后再跑本脚本收尾")

    # 导出 s6b 候选（累积制）：既有候选 ∪ 收敛用到的规则 ∪ 命中改动块
    # 旧文本的规则 ∪ 规则失效型收敛块命中的规则。s6b 消费后剔除已失活项。
    cand_path = os.path.join(STATE_DIR, "adopted_rules.json")
    os.makedirs(STATE_DIR, exist_ok=True)
    cand = defaultdict(set)
    if os.path.exists(cand_path):
        with open(cand_path, encoding="utf-8") as f:
            for c in json.load(f):
                cand[(c["section"], c["old"], c["new"])] |= set(c["rels"])
    for src in (r["rule_use"], r["touched_rules"], r["rulekilled_rules"]):
        for k, rels in src.items():
            cand[k] |= rels
    adopted_rules = [{"section": sec, "old": ro, "new": rn, "rels": sorted(rels)}
                     for (sec, ro, rn), rels in sorted(cand.items())]
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(adopted_rules, f, ensure_ascii=False, indent=1)

    fails = committed = rk_committed = 0
    for rel, ps in pairs_by_rel.items():
        if rel in structural_ok:
            continue  # 结构文件由 s5 --commit 整文件成对提交
        keep = {tuple(xb) for k, xb, _ in ps if k == "adopted" and xb}
        if not keep:
            continue
        xp = "X/" + rel
        inter = apply_blocks(head[xp], work[xp], keep)
        if inter is None:
            print("  定位歧义或含结构改动，整文件留审:", rel)
            fails += 1
            continue
        got = chunk_changes(head[xp], inter.encode("utf-8"))
        if got is None or set(got) != keep:
            print("  中间版本校验失败，整文件留审:", rel)
            fails += 1
            continue
        terms = [t for t in (term_summary(o, n) for o, n in sorted(keep)) if t]
        if dry_run:
            print(f"  [dry-run] X/{rel}（{len(got)} 处）: "
                  + "；".join(terms[:3]) + ("…" if len(terms) > 3 else ""))
            committed += 1
            continue
        body = "\n".join(terms[:6]) + ("\n…" if len(terms) > 6 else "")
        subject = f"fix: sync X/{rel}（{len(got)} 处文本修订，Y 侧规则渲染）"
        with open(xp, "rb") as f:
            saved = f.read()
        try:
            with open(xp, "wb") as f:
                f.write(inter.encode("utf-8"))
            commit_paths(subject, body, [xp])
            committed += 1
        except Exception as ex:  # noqa: BLE001  提交失败回滚留审，继续处理后续文件
            fails += 1
            print("  FAIL:", rel, str(ex)[:150])
        finally:
            with open(xp, "wb") as f:
                f.write(saved)
        git("add", "-A")

    # adopted 提交已推进 HEAD：rulekilled 的中间版本必须以新 HEAD 为基底
    # 重建，否则同文件已提交的 adopted 块会被成对提交静默回滚
    hm2 = head_sha_map()
    cf2 = CatFile()
    for p in (p for rel in pairs_by_rel for p in ("X/" + rel, "Y/" + rel)):
        if p in hm2:
            head[p] = cf2.read(hm2[p])
    cf2.close()

    # 规则失效型收敛块对：成对提交（X/Y 同块，新文本两侧逐字一致）
    for rel, ps in pairs_by_rel.items():
        if rel in structural_ok:
            continue  # 结构文件由 s5 --commit 整文件成对提交
        rk = [(tuple(xb), tuple(yb))
              for k, xb, yb in ps if k == "rulekilled" and xb and yb]
        if not rk:
            continue
        xp, yp = "X/" + rel, "Y/" + rel
        keepx, keepy = {x for x, _ in rk}, {y for _, y in rk}
        ix = apply_blocks(head[xp], work[xp], keepx)
        iy = apply_blocks(head[yp], work[yp], keepy)
        if ix is None or iy is None:
            print("  定位歧义或含结构改动，整文件留审:", rel)
            fails += 1
            continue
        gx = chunk_changes(head[xp], ix.encode("utf-8"))
        gy = chunk_changes(head[yp], iy.encode("utf-8"))
        if gx is None or gy is None or set(gx) != keepx or set(gy) != keepy:
            print("  中间版本校验失败，整文件留审:", rel)
            fails += 1
            continue
        terms = [t for t in (term_summary(*xb) for xb, _ in rk) if t]
        if dry_run:
            print(f"  [dry-run] {rel}（规则失效收敛 {len(rk)} 块）: "
                  + "；".join(terms[:3]) + ("…" if len(terms) > 3 else ""))
            rk_committed += 1
            continue
        body = "\n".join(terms[:6]) + ("\n…" if len(terms) > 6 else "")
        subject = f"fix: sync {rel}（{len(rk)} 处文本修订）"
        saved = {}
        for p in (xp, yp):
            with open(p, "rb") as f:
                saved[p] = f.read()
        try:
            with open(xp, "wb") as f:
                f.write(ix.encode("utf-8"))
            with open(yp, "wb") as f:
                f.write(iy.encode("utf-8"))
            commit_paths(subject, body, [xp, yp])
            rk_committed += 1
        except Exception as ex:  # noqa: BLE001  提交失败回滚留审，继续处理后续文件
            fails += 1
            print("  FAIL:", rel, str(ex)[:150])
        finally:
            for p, b in saved.items():
                with open(p, "wb") as f:
                    f.write(b)
        git("add", "-A")
    git("add", "-A")
    print(f"完成。提交 {committed} 个 X 文件（规则采纳）、"
          f"{rk_committed} 对（规则失效收敛），失败/留审 {fails}，"
          f"规则候选 {len(adopted_rules)} 条（→ s6b）")


if __name__ == "__main__":
    main()
