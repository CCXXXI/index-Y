"""导出失活规则候选（累积制 STATE_DIR/adopted_rules.json），供 report_inactive_rules 消费。

候选 = 既有候选 ∪ 收敛用到的规则 ∪ 命中改动块旧文本的规则 ∪ 规则失效型收敛块
命中的规则，覆盖「上游采纳」与「上游改写源文本为第三种形式」两种失效路径。
原子提交模型下本脚本只导出不提交：相关改动随 triage_text --commit 一笔入仓，
report_inactive_rules 在其后验证（HEAD 已含改动才能判失活，顺序强制）。
幂等可重跑。用法: uv run python scripts/sync/export_rule_candidates.py
"""

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commit_image_renames import STATE_DIR
from lib_triage import triage_parser
from triage_text import classify


def main() -> None:
    triage_parser(__doc__).parse_args()
    r = classify()
    cand_path = os.path.join(STATE_DIR, "adopted_rules.json")
    os.makedirs(STATE_DIR, exist_ok=True)
    cand: dict[tuple, set] = defaultdict(set)
    if os.path.exists(cand_path):
        with open(cand_path, encoding="utf-8") as f:
            for c in json.load(f):
                cand[(c["section"], c["old"], c["new"])] |= set(c["rels"])
    for src in (r["rule_use"], r["touched_rules"], r["rulekilled_rules"]):
        for k, rels in src.items():
            cand[k] |= rels
    adopted_rules = [
        {"section": sec, "old": ro, "new": rn, "rels": sorted(rels)}
        for (sec, ro, rn), rels in sorted(cand.items())
    ]
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(adopted_rules, f, ensure_ascii=False, indent=1)
    print(
        f"失活规则候选 {len(adopted_rules)} 条 → {cand_path}"
        "（report_inactive_rules 消费）"
    )


if __name__ == "__main__":
    main()
