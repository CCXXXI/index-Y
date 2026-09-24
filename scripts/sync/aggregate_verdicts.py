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
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import repo_root, triage_parser


def norm_jp(s: str) -> str:
    """归并空白并剥页码前缀〔…〕，供 jp 引用逐字子串比对。"""
    return re.sub(r"〔[^〕]*〕|\s+", "", s)


def main() -> int:
    triage_parser(__doc__).parse_args()
    root = repo_root()
    triage = os.path.join(root, ".triage")
    with open(os.path.join(triage, "review_blocks.json"), encoding="utf-8") as f:
        blocks = json.load(f)
    # 兼容无 id 字段的旧格式：id 即块在材料中的位置（与 parse_blocks 一致）
    want = {b.get("id", i): b for i, b in enumerate(blocks)}

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
    print(
        f"块总数 {len(want)}，已收 verdict {len(verdicts)}，"
        f"缺 {len(missing)}，坏行 {len(bad)}"
    )
    if missing:
        print("缺 id:", missing[:50])
    for t in bad[:20]:
        print("坏行", t)

    # jp 引用真实性核验（子代理可能编造原文证据）：逐条查该卷 jp_text
    jp_cache: dict[str, str | None] = {}
    cited = jp_bad = 0
    for i, v in sorted(verdicts.items()):
        jp = v.get("jp", "")
        if not jp:
            continue
        cited += 1
        vol = want[i]["files"][0].split("]")[0].strip("[")
        if vol not in jp_cache:
            p = os.path.join(triage, "jp_text", f"{vol}.txt")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    jp_cache[vol] = norm_jp(f.read())
            else:
                jp_cache[vol] = None
        src = jp_cache[vol]
        if src is None:
            bad.append(f"id {i}: 卷 {vol} 无 jp_text 而 jp 引用非空")
            jp_bad += 1
            continue
        parts = [norm_jp(x) for x in re.split(r"…|\.{3}", jp) if x.strip()]
        if any(x not in src for x in parts):
            bad.append(f"id {i}: jp 引用不在 {vol} 原文中（疑似编造）: {jp[:60]}")
            jp_bad += 1
    print(f"jp 引用核验: {cited} 条, 不在原文 {jp_bad} 条")
    for t in bad[-jp_bad:][:20] if jp_bad else []:
        print("坏引用", t)

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
    return 1 if (missing or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
