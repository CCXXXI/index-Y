"""批次 5：文本改动逐文件分流（类别 1 每文件一个 commit）。

分类（按相对路径配对 X/<rel> 与 Y/<rel>，块级提取 + 片段级比较）：
- same       两侧改动集一致 → 类别 1 候选
- diff       块级不一致但片段级一致 → 类别 1 候选（旧 Y 同句含规则改写）
- x_only     类别 3（上游做了规则已覆盖的改动）→ 留下
- complex    文本增删/对齐失败 → 留下
- 其余        疑类别 2 → 留下

运行两次：
1) 默认模式：只分类并导出审查材料（STATE_DIR/review_changes.txt + plan.json），
   人工/AI 审查后把可疑改动（整块旧/新文本）填入 STATE_DIR/triage_exclude.json。
2) --commit：提交未被排除的候选对。
用法: uv run python scripts/s5_triage_text.py [--commit]
"""

import difflib
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import (TEXT_EXT, CatFile, chunk_changes, commit_paths,
                        frag_set, git, head_sha_map)  # noqa: E402
from s1_commit_image_renames import STATE_DIR  # noqa: E402


def term_summary(o: str, n: str, maxlen: int = 40) -> str:
    sm = difflib.SequenceMatcher(a=o, b=n, autojunk=False)
    outs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = o[i1:i2], n[j1:j2]
        outs.append(f"{a[:maxlen]}→{b[:maxlen]}")
    return "；".join(outs)


def main() -> None:
    do_commit = "--commit" in sys.argv
    git("add", "-A")
    entries = git("status", "--porcelain", "-z").decode("utf-8").split("\0")
    mfiles = [e[3:] for e in entries
              if e and e[0] == "M" and not e.startswith("R")
              and e[3:].lower().endswith(TEXT_EXT)]

    hm = head_sha_map()
    cf = CatFile()
    changes: dict[str, list] = {}
    complex_files = set()
    for path in mfiles:
        ch = chunk_changes(cf.read(hm[path]),
                           open(path, "rb").read())
        if ch is None:
            complex_files.add(path)
        else:
            changes[path] = ch
    cf.close()

    groups = defaultdict(dict)
    for p in mfiles:
        root, rel = p.split("/", 1)
        groups[rel][root] = p

    plan = {"same": [], "diff_ok": [], "diff_bad": [], "x_only": [],
            "y_only": [], "complex": []}
    for rel, d in groups.items():
        xp, yp = d.get("X"), d.get("Y")
        xc = changes.get(xp) if xp else None
        yc = changes.get(yp) if yp else None
        if (xp in complex_files) or (yp in complex_files):
            plan["complex"].append(rel)
        elif xc and yc:
            if sorted(xc) == sorted(yc):
                plan["same"].append(rel)
            elif frag_set(xc) == frag_set(yc):
                plan["diff_ok"].append(rel)
            else:
                plan["diff_bad"].append(rel)
        elif xc:
            plan["x_only"].append(rel)
        elif yc:
            plan["y_only"].append(rel)

    for k, v in plan.items():
        print(f"{len(v):5d}  {k}")

    # 导出审查材料：候选的全部去重改动
    os.makedirs(STATE_DIR, exist_ok=True)
    agg = Counter()
    for rel in plan["same"] + plan["diff_ok"]:
        for o, n in changes["X/" + rel]:
            agg[(o, n)] += 1
    with open(os.path.join(STATE_DIR, "review_changes.txt"), "w",
              encoding="utf-8") as f:
        for (o, n), c in agg.most_common():
            f.write(f"[{c}次]\n- {o}\n+ {n}\n\n")
    json.dump(plan, open(os.path.join(STATE_DIR, "plan.json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"审查材料: {os.path.join(STATE_DIR, 'review_changes.txt')}"
          f"（{sum(agg.values())} 块 / 去重 {len(agg)}）")

    if not do_commit:
        print("审查后把可疑改动（整块 old/new）写入 "
              f"{os.path.join(STATE_DIR, 'triage_exclude.json')}，再带 --commit 运行")
        return

    excl_path = os.path.join(STATE_DIR, "triage_exclude.json")
    excludes = set()
    if os.path.exists(excl_path):
        excludes = {tuple(x) for x in json.load(open(excl_path, encoding="utf-8"))}
        print(f"排除规则 {len(excludes)} 条")

    rejected, accepted = [], []
    for rel in plan["same"] + plan["diff_ok"]:
        ch = changes["X/" + rel]
        if any(tuple(c) in excludes for c in ch):
            rejected.append(rel)
        else:
            accepted.append(rel)
    print(f"提交 {len(accepted)} 对，剔除 {len(rejected)} 对")
    for rel in rejected:
        print("  剔除:", rel)

    fails = 0
    for rel in accepted:
        ch = changes["X/" + rel]
        if rel.lower().endswith((".opf", ".ncx")):
            subject, body = f"chore: sync {rel}", "元数据/时间戳更新"
        else:
            terms = [t for t in (term_summary(o, n) for o, n in ch) if t]
            body = "\n".join(terms[:6]) + ("\n…" if len(terms) > 6 else "")
            subject = f"fix: sync {rel}（{len(ch)} 处文本修订）"
        try:
            commit_paths(subject, body, ["X/" + rel, "Y/" + rel])
        except Exception as ex:
            fails += 1
            print("  FAIL:", rel, str(ex)[:150])
    git("add", "-A")
    left = [l for l in git("status", "--porcelain").decode("utf-8").splitlines() if l]
    print(f"完成。失败 {fails}，剩余暂存 {len(left)}")


if __name__ == "__main__":
    main()
