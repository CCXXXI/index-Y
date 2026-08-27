"""批次 6b：删除已被上游采纳的冗余规则（机械验证，无需人工）。

候选 = STATE_DIR/adopted_rules.json（s6a 导出：收敛用到的规则 ∪ 命中改动块
旧文本的规则，覆盖「上游采纳」与「上游改写源文本为第三种形式」两种失效
路径）。对每条候选：
从 fixes 中移除该条后 fixed() 在【HEAD 与工作区】两侧 X 上输出均不变
→ 冗余，从 x2y.py 删除（AST 定位，兼容 rf 串与多行元组）；否则保留并报告
（规则仍在他处生效，部分冗余）。

验证 HEAD X 同时强制执行顺序：s6a 未先提交时 HEAD X 仍是旧版，验证会失败。
验证工作区 X 是为了覆盖疑似上游错误块——它们留在工作区，可能仍命中待删规则，
删掉会让 check_y_freshness（比工作区）误报滞后。

默认只报告；--commit 才修改 x2y.py 并提交（单个 commit），提交前做全树
差分复核（新旧管道输出逐字节一致）。
用法: uv run python scripts/s6b_prune_redundant_rules.py [--commit]
"""

import ast
import json
import os
import sys

import regex as re  # 规则含 \p{}，stdlib re 不支持

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib_triage import TEXT_EXT, CatFile, commit_paths, head_sha_map, repo_root  # noqa: E402
from s1_commit_image_renames import STATE_DIR  # noqa: E402
from x2y import NON_END, fixes  # noqa: E402

X2Y = os.path.join(repo_root(), "x2y.py")


def rule_nodes():
    """x2y.py 中 fixes 的规则节点索引：{(section, old, new): [(lineno, end)]}。

    对 Tuple 节点 eval 求值（命名空间只给 NON_END），兼容字面值与 rf 串。
    """
    tree = ast.parse(open(X2Y, encoding="utf-8").read())
    out = {}
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign):
            target = next((t for t in node.targets
                           if isinstance(t, ast.Name) and t.id == "fixes"),
                          None)
            value = node.value
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)
              and node.target.id == "fixes"):
            target = node.target
            value = node.value
        if target is None or not isinstance(value, ast.Dict):
            continue
        for k, v in zip(value.keys, value.values):
            if not (isinstance(k, ast.Constant) and isinstance(v, ast.List)):
                continue
            for elt in v.elts:
                if not isinstance(elt, ast.Tuple):
                    continue
                try:
                    val = eval(compile(ast.Expression(elt), X2Y, "eval"),
                               {"NON_END": NON_END})
                except Exception:
                    continue
                out.setdefault((k.value, *val), []).append(
                    (elt.lineno, elt.end_lineno))
    return out


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


def main() -> int:
    do_commit = "--commit" in sys.argv
    cand_path = os.path.join(STATE_DIR, "adopted_rules.json")
    if not os.path.exists(cand_path):
        print("无候选：先运行 s6a_commit_adopted_x.py")
        return 1
    cands = json.load(open(cand_path, encoding="utf-8"))

    nodes = rule_nodes()
    files = x_text_files()
    hm = head_sha_map()
    cf = CatFile()
    root = repo_root()

    redundant, active, missing = [], [], []
    for c in cands:
        key = (c["section"], c["old"], c["new"])
        if key not in nodes:
            missing.append(c)
            continue
        vols = sorted(files) if c["section"] == "*" else [c["section"]]
        counter = None
        for vol in vols:
            for rel in files.get(vol, []):
                # 工作区与 HEAD 两侧都要不触发/输出不变
                contents = [open(os.path.join(root, "X", rel),
                                 encoding="utf-8").read()]
                sha = hm.get("X/" + rel.replace(os.sep, "/"))
                if sha:
                    contents.append(cf.read(sha).decode("utf-8"))
                if any(differs_without(vol, cnt, key)
                       for cnt in contents):
                    counter = rel
                    break
            if counter:
                break
        (active if counter else redundant).append(
            (c, counter) if counter else c)
    cf.close()

    print(f"{len(redundant):3d} 条已验证冗余（可删除）")
    for c in redundant:
        print(f"    [{c['section']}] {c['old']} -> {c['new']}"
              f"（{len(c['rels'])} 处采纳）")
    if active:
        print(f"{len(active):3d} 条仍生效（保留，首个反例）：")
        for c, rel in active:
            print(f"    [{c['section']}] {c['old']} -> {c['new']}  @ {rel}")
    if missing:
        print(f"{len(missing):3d} 条在 x2y.py 中未找到（可能已处理）：")
        for c in missing:
            print(f"    [{c['section']}] {c['old']} -> {c['new']}")

    if not redundant:
        return 0
    if not do_commit:
        print("\n确认后带 --commit 运行以删除并提交")
        return 0

    lines = open(X2Y, encoding="utf-8").readlines()
    spans = sorted((nodes[(c["section"], c["old"], c["new"])][0]
                    for c in redundant), reverse=True)
    for lo, hi in spans:
        del lines[lo - 1:hi]
    with open(X2Y, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # 最终门禁：重载 x2y 后，新管道在【HEAD 与工作区】X 上的输出必须与
    # 旧管道逐字节一致（差分校验）。不直接比 Y：HEAD 可能存在分流中途的
    # 既有不对称（如 Y 侧待提交文件），与本次删除无关。
    import importlib
    import x2y
    importlib.reload(x2y)
    bad = 0
    cf = CatFile()
    for vol, rels in files.items():
        for rel in rels:
            p = rel.replace(os.sep, "/")
            contents = [open(os.path.join(root, "X", rel),
                             encoding="utf-8").read()]
            sha = hm.get("X/" + p)
            if sha:
                contents.append(cf.read(sha).decode("utf-8"))
            if any(x2y.fixed(vol, cnt) != fixed_with(fixes, vol, cnt)
                   for cnt in contents):
                print("  输出变化:", p)
                bad += 1
    cf.close()
    if bad:
        print(f"最终校验失败（{bad} 个文件），x2y.py 已改但未提交，请检查")
        return 1

    body = "\n".join(f"- [{c['section']}] {c['old']} -> {c['new']}"
                     for c in redundant)
    commit_paths(f"chore: drop upstream-adopted rules（{len(redundant)} 条）",
                 body, ["x2y.py"])
    print(f"已提交：删除 {len(redundant)} 条冗余规则")
    # 累积制候选的消费剔除：已删除与已失效（missing）的移出 json，
    # 仍生效的保留（后续批次相关块落地后可再次验证）
    consumed = {(c["section"], c["old"], c["new"])
                for c in redundant} | {(c["section"], c["old"], c["new"])
                                       for c in missing}
    left = [c for c in cands
            if (c["section"], c["old"], c["new"]) not in consumed]
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(left, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
