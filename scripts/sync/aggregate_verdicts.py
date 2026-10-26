"""聚合子代理审查产出：校验完整性，导出需人工复核的块。

读取 .triage/verdicts/*.jsonl 与 .triage/review_blocks.json：
- 校验：行可解析、id 无重复、覆盖全部块（缺漏即列出，须重派或补审）
- 核验：semantic 块的 jp 引用逐字存在于该卷 jp_text（省略号分段逐段比对，
  防子代理编造原文证据；块所在卷无 jp_text 而 jp 非空同样报告）
- 汇总 verdict 分布
- 导出 verdict_suspect.json / verdict_unsure.json / verdict_unlocated.json
  （含块原文与子代理理由/jp 证据），供逐条人工复核

注意：子代理 verdict 是自报结论，suspect/unsure/unlocated 全部、ok 抽查
复核后才可放行或写规则（.agents/skills/sync-triage/SKILL.md §6）。

用法: uv run python scripts/sync/aggregate_verdicts.py
"""

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import verdict_io
from lib_triage import repo_root, triage_parser


def main() -> int:
    triage_parser(__doc__).parse_args()
    root = repo_root()
    triage = os.path.join(root, ".triage")
    with open(os.path.join(triage, "review_blocks.json"), encoding="utf-8") as f:
        blocks = json.load(f)
    # 兼容无 id 字段的旧格式：id 即块在材料中的位置（与 parse_blocks 一致）
    want = {b.get("id", i): b for i, b in enumerate(blocks)}
    try:
        verdicts = verdict_io.load_verdicts(triage, blocks)
    except verdict_io.VerdictError as exc:
        print(f"verdict 校验失败：\n{exc}")
        return 1

    missing = sorted(set(want) - set(verdicts))
    print(f"块总数 {len(want)}，已收 verdict {len(verdicts)}，缺 {len(missing)}")
    if missing:
        print("缺 id:", missing[:50])
        return 1

    cited = sum(bool(v["jp"].strip()) for v in verdicts.values())
    print(f"jp 引用核验: {cited} 条，全部有效")
    print("verdict 分布:", dict(Counter(v["verdict"] for v in verdicts.values())))
    for kind in ("suspect", "unsure", "unlocated"):
        items = [
            {
                "id": i,
                "file": want[i]["files"],
                "old": want[i]["old"],
                "new": want[i]["new"],
                "cls": v.get("cls", ""),
                "jp": v.get("jp", ""),
                "reason": v.get("reason", ""),
            }
            for i, v in sorted(verdicts.items())
            if v["verdict"] == kind
        ]
        dst = os.path.join(triage, f"verdict_{kind}.json")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        print(f"{kind}: {len(items)} -> {os.path.relpath(dst, root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
