"""聚合子代理审查产出：校验完整性，导出需人工复核的块。

读取 .triage/verdicts/*.jsonl 与 .triage/review_blocks.json：
- 校验：行可解析、id 无重复、覆盖全部块（缺漏即列出，须重派或补审）
- 汇总 verdict 分布
- 导出 verdict_suspect.json / verdict_unsure.json / verdict_unlocated.json
  （含块原文与子代理理由/jp 证据），供逐条人工复核

注意：子代理 verdict 是自报结论，suspect/unsure/unlocated 全部、ok 抽查
复核后才可放行或写规则（docs/sync-triage.md §6）。

用法: uv run python scripts/sync/aggregate_verdicts.py
"""

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import repo_root, triage_parser


def main() -> int:
    triage_parser(__doc__).parse_args()
    root = repo_root()
    triage = os.path.join(root, ".triage")
    with open(os.path.join(triage, "review_blocks.json"), encoding="utf-8") as f:
        want = {b["id"]: b for b in json.load(f)}

    verdicts: dict[int, dict] = {}
    bad = []
    vdir = os.path.join(triage, "verdicts")
    files = sorted(f for f in os.listdir(vdir) if f.endswith(".jsonl"))
    for fname in files:
        with open(os.path.join(vdir, fname), encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    v = json.loads(line)
                except json.JSONDecodeError:
                    bad.append(f"{fname}:{ln}: JSON 不可解析")
                    continue
                if v["id"] in verdicts:
                    bad.append(f"{fname}:{ln}: 重复 id {v['id']}")
                    continue
                verdicts[v["id"]] = v

    missing = sorted(set(want) - set(verdicts))
    print(f"块总数 {len(want)}，已收 verdict {len(verdicts)}，"
          f"缺 {len(missing)}，坏行 {len(bad)}")
    if missing:
        print("缺 id:", missing[:50])
    for t in bad[:20]:
        print("坏行", t)

    print("verdict 分布:", dict(Counter(v["verdict"] for v in verdicts.values())))
    for kind in ("suspect", "unsure", "unlocated"):
        items = [{
            "id": i, "file": want[i]["files"],
            "old": want[i]["old"], "new": want[i]["new"],
            "cls": v.get("cls", ""), "jp": v.get("jp", ""),
            "reason": v.get("reason", ""),
        } for i, v in sorted(verdicts.items()) if v["verdict"] == kind]
        dst = os.path.join(triage, f"verdict_{kind}.json")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        print(f"{kind}: {len(items)} -> {os.path.relpath(dst, root)}")
    return 1 if (missing or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
