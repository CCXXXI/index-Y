"""批次 6a：提交 X 侧规则采纳改动（上游采纳既有规则，仅 X 有改动）。

无需等人工审查，s5 分类后即可运行；s5 --commit 之后应再跑一遍收尾——
此时混合块对的正常同步片段已入 HEAD，重跑分类后其 X 侧剩余 diff 退化为
纯规则采纳块。本脚本只处理 kind == "adopted" 的整块，无需片段级逻辑。
提交粒度为块：apply_blocks(keep=规则采纳块) 构造中间版本，正常同步候选、
疑似上游错误块自动留在工作区。

收敛判定（convergent）保证提交后 HEAD 上 x2y(X) == Y 不变式不被破坏。
同时将 rule_use 导出为 STATE_DIR/adopted_rules.json，供 s6b 验证删除冗余
规则。用法: uv run python scripts/s6a_commit_adopted_x.py [--dry-run]
"""

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib_triage import apply_blocks, chunk_changes, commit_paths, git  # noqa: E402
from s1_commit_image_renames import STATE_DIR  # noqa: E402
from s5_triage_text import classify, term_summary  # noqa: E402


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    r = classify()
    pairs_by_rel, head, work = r["pairs_by_rel"], r["head"], r["work"]

    if any(k in ("sync", "mixed")
           for ps in pairs_by_rel.values() for k, _, _ in ps):
        print("提示: 存在正常同步候选/混合块，混合块的规则采纳片段需等 "
              "s5 --commit 后再跑本脚本收尾")

    # 导出 s6b 候选：收敛用到的规则 ∪ 命中改动块旧文本的规则
    # （上游改写源文本为第三种形式致规则失效的情形由后者覆盖）
    os.makedirs(STATE_DIR, exist_ok=True)
    cand = defaultdict(set)
    for src in (r["rule_use"], r["touched_rules"]):
        for k, rels in src.items():
            cand[k] |= rels
    adopted_rules = [{"section": sec, "old": ro, "new": rn, "rels": sorted(rels)}
                     for (sec, ro, rn), rels in sorted(cand.items())]
    with open(os.path.join(STATE_DIR, "adopted_rules.json"), "w",
              encoding="utf-8") as f:
        json.dump(adopted_rules, f, ensure_ascii=False, indent=1)

    fails = committed = 0
    for rel, ps in pairs_by_rel.items():
        keep = {tuple(xb) for k, xb, _ in ps if k == "adopted" and xb}
        if not keep:
            continue
        xp = "X/" + rel
        inter = apply_blocks(head[xp], work[xp], keep)
        if inter is None:
            print("  定位歧义，整文件留审:", rel)
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
        subject = f"fix: sync X/{rel}（上游采纳规则，{len(got)} 处）"
        saved = open(xp, "rb").read()
        try:
            with open(xp, "wb") as f:
                f.write(inter.encode("utf-8"))
            commit_paths(subject, body, [xp])
            committed += 1
        except Exception as ex:
            fails += 1
            print("  FAIL:", rel, str(ex)[:150])
        finally:
            with open(xp, "wb") as f:
                f.write(saved)
        git("add", "-A")
    git("add", "-A")
    print(f"完成。提交 {committed} 个 X 文件，失败/留审 {fails}，"
          f"规则候选 {len(adopted_rules)} 条（→ s6b）")


if __name__ == "__main__":
    main()
