"""批次 6b：报告当前已失活的规则（纯报告，不删除）。

候选 = STATE_DIR/adopted_rules.json（s6a 导出：收敛用到的规则 ∪ 命中改动块
旧文本的规则，覆盖「上游采纳」与「上游改写源文本为第三种形式」两种失效
路径）。对每条候选：从 fixes 中移除该条后 fixed() 在【HEAD 与工作区】两侧
X 上输出均不变 → 当前失活；否则仍生效（报告首个反例）。

失活规则一律保留在 rules/ 中不删除：X 持续增长，错误类规则（如「荧幕」）
可能在新卷内容上复发，删除会把未来的漏校正变成人工审查成本；保留的全语料
扫描开销可忽略（每条规则约十几毫秒）。本报告仅供人工参考。

验证 HEAD X 同时强制执行顺序：s6a 未先提交时 HEAD X 仍是旧版，验证会失败
（报「仍生效」）。验证工作区 X 是为了覆盖疑似上游错误块——它们留在工作区，
可能仍命中候选规则。

已验证失活与已不在 rules/ 中的候选从 json 剔除；仍生效的保留（后续批次
相关块落地后可再次验证）。
用法: uv run python scripts/sync/s6b_report_inactive_rules.py
"""

import json
import os
import sys

import regex as re  # 规则含 \p{}，stdlib re 不支持

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib_triage import (
    TEXT_EXT,
    CatFile,
    head_sha_map,
    repo_root,
    triage_parser,
)
from s1_commit_image_renames import STATE_DIR
from x2y import fixes


def fixed_with(rules: dict, vol: str, content: str) -> str:
    for old, new in rules.get(vol, []) + rules["*"]:
        content = re.sub(old, new, content)
    return content


def differs_without(vol: str, content: str, skip: tuple) -> bool:
    """从 vol 的管道中跳过 skip=(section, old, new) 的首个出现，输出是否变化。

    快速路径：规则在其管道位置不触发 → 跳过是恒等操作，输出必然不变，
    不必跑完后缀（可靠否定）；触发过则跑完两条管道对比最终输出
    （排除后缀规则再收敛的情形）。
    """
    sec, ro, rn = skip
    seq = ([(vol, *t) for t in fixes.get(vol, [])]
           + [("*", *t) for t in fixes["*"]])
    skipped = fired = False
    out = content
    for s, o, n in seq:
        if not skipped and (s, o, n) == (sec, ro, rn):
            skipped = True
            if re.sub(o, n, out) == out:
                return False  # 管道位置不触发 → 输出不变
            fired = True
            continue
        out = re.sub(o, n, out)
    return fired and out != fixed_with(fixes, vol, content)


def x_text_files():
    """{vol: [相对 X/ 的路径]}。"""
    root = repo_root()
    out = {}
    xdir = os.path.join(root, "X")
    for vol in sorted(os.listdir(xdir)):
        vdir = os.path.join(xdir, vol)
        if not os.path.isdir(vdir):
            continue
        rels = []
        for dirpath, _, files in os.walk(vdir):
            for name in files:
                if name.lower().endswith(TEXT_EXT):
                    rels.append(os.path.relpath(
                        os.path.join(dirpath, name), xdir))
        out[vol] = rels
    return out


def rule_present(key: tuple) -> bool:
    sec, old, new = key
    return (old, new) in fixes.get(sec, [])


def main() -> int:
    triage_parser(__doc__).parse_args()
    cand_path = os.path.join(STATE_DIR, "adopted_rules.json")
    if not os.path.exists(cand_path):
        print("无候选：先运行 s6a_commit_adopted_x.py")
        return 1
    with open(cand_path, encoding="utf-8") as f:
        cands = json.load(f)

    files = x_text_files()
    hm = head_sha_map()
    cf = CatFile()
    root = repo_root()

    inactive, active, missing = [], [], []
    for c in cands:
        key = (c["section"], c["old"], c["new"])
        if not rule_present(key):
            missing.append(c)
            continue
        vols = sorted(files) if c["section"] == "*" else [c["section"]]
        counter = None
        for vol in vols:
            for rel in files.get(vol, []):
                # 工作区与 HEAD 两侧都要不触发/输出不变
                with open(os.path.join(root, "X", rel), encoding="utf-8") as fp:
                    contents = [fp.read()]
                sha = hm.get("X/" + rel.replace(os.sep, "/"))
                if sha:
                    contents.append(cf.read(sha).decode("utf-8"))
                if any(differs_without(vol, cnt, key)
                       for cnt in contents):
                    counter = rel
                    break
            if counter:
                break
        (active if counter else inactive).append(
            (c, counter) if counter else c)
    cf.close()

    print(f"{len(inactive):3d} 条当前失活（上游已收敛；保留不删，仅供参考）")
    for c in inactive:
        print(f"    [{c['section']}] {c['old']} -> {c['new']}"
              f"（{len(c['rels'])} 处采纳）")
    if active:
        print(f"{len(active):3d} 条仍生效（首个反例）：")
        for c, rel in active:
            print(f"    [{c['section']}] {c['old']} -> {c['new']}  @ {rel}")
    if missing:
        print(f"{len(missing):3d} 条不在 rules/ 中：")
        for c in missing:
            print(f"    [{c['section']}] {c['old']} -> {c['new']}")

    # 累积制候选的消费剔除：已失活与已缺失的移出 json，仍生效的保留
    consumed = {(c["section"], c["old"], c["new"])
                for c in inactive} | {(c["section"], c["old"], c["new"])
                                      for c in missing}
    left = [c for c in cands
            if (c["section"], c["old"], c["new"]) not in consumed]
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(left, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
