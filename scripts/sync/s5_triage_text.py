"""批次 5：文本改动按【改动块】分流（正常同步提交，疑似上游错误/规则采纳留下）。

分类（按相对路径配对 X/<rel> 与 Y/<rel>）：
1. 两侧各自提取相对 HEAD 的文本改动（文本块级 difflib，仅接受 1:1 replace）。
2. 旧 X 与旧 Y 的文本块序列做【位置对齐】（1:1，允许旧 Y 被规则改写），
   把两侧改动配成「同位置块对」。位置对齐失败 → 复杂，整文件留下。
   含块增删/N→M 替换的结构改动文件走 structural_pairs 块组级配对：
   以「映射后旧块锚点 + 操作类型 + 组长度」配对两侧 difflib 组并做内容
   收敛验证；全部块对为 sync/rulekilled 时整文件成对提交（无法按块拆分），
   否则整文件留审。
3. 逐块对分类（F() = 块内最小差异片段集）：
   - 两侧都有改动且 F(x) == F(y)          → 正常同步候选对（"sync"）
   - 两侧都有改动且 F(x) ⊃ F(y)，X 多出的片段能被 x2y 规则解释（收敛）
                                           → 混合块对（"mixed"）：Y 块整块提交；
                                             X 侧片段级拆分，正常同步片段提交、
                                             规则采纳片段留下（两侧提交保持对称，
                                             HEAD 上 x2y(X) == Y 不变式不被破坏）
   - 仅 X 有改动且能收敛（或规则管道等价：apply_rules 后新旧 X 文本一致，
     即上游改动整个落在规则覆盖内、fixed 后 Y 不变——覆盖多步推导这类
     片段级收敛看不见的情形）       → 规则采纳（"adopted"，留 s6a）
   - 其余（Y 有多余片段 / 仅 Y 有改动 / 不收敛）→ 疑似上游错误（"suspect"，留下）
   片段级比较是必要的：旧 Y 的同一句子可能已被 x2y 规则改过，
   块字面不同不代表改动不同；但只做全局片段集比较会把「同碎片的另一处
   规则采纳改动」误判为正常同步，所以必须先按位置配对。

提交粒度为块而非文件：用 apply_blocks 构造「只应用正常同步块」的中间版本提交，
规则采纳/疑似块与被排除块留在工作区。排除（triage_exclude.json）按块对生效：
任一侧块命中排除则整对留下。每文件一个 commit（X/Y 成对）。

运行两次：
1) 默认模式：分类并导出审查材料（STATE_DIR/review_changes.txt = 正常同步候选块、
   suspect_changes.txt = 疑似上游错误块、plan.json = 逐文件块对明细）。
2) --commit：提交未被排除的正常同步块。
用法: uv run python scripts/sync/s5_triage_text.py [--commit]
"""

import difflib
import json
import os
import sys
from collections import Counter, defaultdict

import regex as re  # 规则含 \p{}，stdlib re 不支持

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib_triage import (TEXT_EXT, CatFile, aligned_chunks, apply_blocks,  # noqa: E402
                        apply_frags, chunk_changes, commit_paths, frag_set,
                        git, head_sha_map, norm_ws, raw_variants, text_chunks)
from s1_commit_image_renames import STATE_DIR  # noqa: E402
from x2y import fixes, fixed  # noqa: E402


def apply_rules(vol: str, s: str) -> str:
    """对文本应用该卷全部 x2y 规则（fixed 的规则部分），供收敛验证。"""
    for ro, rn in fixes.get(vol, []) + fixes["*"]:
        s = re.sub(ro, rn, s)
    return s


def term_summary(o: str, n: str, maxlen: int = 40) -> str:
    sm = difflib.SequenceMatcher(a=o, b=n, autojunk=False)
    outs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        outs.append(f"{o[i1:i2][:maxlen]}→{n[j1:j2][:maxlen]}")
    return "；".join(outs)


def group_summary(o: str, n: str, maxlen: int = 40) -> str:
    """块/组的提交信息摘要；结构组的增删一侧为空串。"""
    if not o:
        return f"新增块: {n[:maxlen]}"
    if not n:
        return f"删除块: {o[:maxlen]}"
    return term_summary(o, n, maxlen)


def block_frags(o: str, n: str) -> set:
    return set(frag_set([(o, n)]))


def indexed_changes(old: bytes, new: bytes):
    """[(旧块索引, 旧块, 新块)]（norm_ws 形态）；有增删返回 None。"""
    al = aligned_chunks(old, new)
    if al is None:
        return None
    oc, nc, ops = al
    on = [norm_ws(c) for c in oc]
    nn = [norm_ws(c) for c in nc]
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        for oi, nj in zip(range(i1, i2), range(j1, j2)):
            out.append((oi, on[oi], nn[nj]))
    return out


def positional_map(xhead: bytes, yhead: bytes, vol: str | None = None):
    """旧 X 块索引 → 旧 Y 块索引（允许旧 Y 被规则 1:1 改写）；失败返回 None。

    规则替换文本可注入/消除带文本的标记（如 <ruby><rt>），使 Y 侧块数与
    X 不等：此类增删块在 HEAD 与工作中等量存在，不影响对齐——但仅在
    HEAD 上 fixed(X) == Y（换行归一化）成立时容忍（证明增删是规则产物），
    否则视为真复杂返回 None。被规则消除的 X 块不进入映射（pmap.get → None）。
    """
    xn = [norm_ws(c) for c in text_chunks(xhead)]
    yn = [norm_ws(c) for c in text_chunks(yhead)]
    sm = difflib.SequenceMatcher(a=xn, b=yn, autojunk=False)
    ops = sm.get_opcodes()
    tolerated = []  # insert/delete 组（疑似规则注入/消除的块）
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            continue
        if tag in ("insert", "delete"):
            tolerated.append(tag)
            continue
        return None  # 不等长 replace：真复杂
    if tolerated:
        if vol is None:
            return None
        xl = xhead.decode("utf-8").replace("\r\n", "\n")
        yl = yhead.decode("utf-8").replace("\r\n", "\n")
        if fixed(vol, xl) != yl:
            return None  # HEAD 不变式不成立，增删块来源无法证明是规则
    m = {}
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            for k in range(i2 - i1):
                m[i1 + k] = j1 + k
    return m


def classify_block(vol: str, rules: list, xo: str, xn: str,
                   yo: str, yn: str):
    """1:1 块对分类。返回 (kind, used, fired)：
    used = 收敛判定用到的规则；fired = rulekilled 块命中旧文本的规则。"""
    fx, fy = block_frags(xo, xn), block_frags(yo, yn)
    if fx == fy:
        return "sync", [], []
    if xn == yn and not any(re.search(r[1], xn) for r in rules):
        # 规则失效型收敛：旧 X 命中规则（两侧分叉由规则造成），
        # 上游改写/消除触发文本后两侧逐字收敛且无规则命中。
        # 必是 rule 命中旧文本（否则 xo==yo、fx==fy 走 sync 了）
        fired = [r for r in rules if re.search(r[1], xo)]
        return ("rulekilled", [], fired) if fired else ("suspect", [], [])
    if fy < fx:
        used: list = []
        if convergent(vol, xo, xn, fy, used):
            return "mixed", used, []  # X 块留下，Y 块提交
    # Y 侧多出片段：规则因上游编辑获得触发语境（如编辑引入「所以」触发
    # 由于→因为）。fixed 能分别从新旧 X 文本得到新旧 Y 文本，则两侧差异
    # 纯属规则渲染、X 改动是真实的上游编辑 → sync
    if apply_rules(vol, xo) == yo and apply_rules(vol, xn) == yn:
        return "sync", [], []
    return "suspect", [], []


def structural_pairs(vol: str, rules: list, xh: bytes, xw: bytes,
                     yh: bytes, yw: bytes, pmap: dict):
    """结构改动文件（块增删 / N→M 替换）的块组级配对分类。

    以「映射后的旧块锚点 + 操作类型 + 组长度」配对两侧 difflib 组，配对的
    组做内容收敛验证（X 组文本应用规则后 == Y 组文本）；1:1 组逐块走块级
    分类。返回 (pairs, gate_ok, used_rules, fired_rules)。
    gate_ok = 全部块对为 sync/rulekilled——结构文件无法按块拆分提交，
    仅在全通过时整文件成对提交，否则整文件留审。
    """
    xoc = [norm_ws(c) for c in text_chunks(xh)]
    xnc = [norm_ws(c) for c in text_chunks(xw)]
    yoc = [norm_ws(c) for c in text_chunks(yh)]
    ync = [norm_ws(c) for c in text_chunks(yw)]
    xops = [op for op in difflib.SequenceMatcher(a=xoc, b=xnc, autojunk=False)
            .get_opcodes() if op[0] != "equal"]
    yops = [op for op in difflib.SequenceMatcher(a=yoc, b=ync, autojunk=False)
            .get_opcodes() if op[0] != "equal"]
    y_by_key = defaultdict(list)
    for t, i1, i2, j1, j2 in yops:
        y_by_key[(i1, t, i2 - i1, j2 - j1)].append((t, i1, i2, j1, j2))

    pairs, gate = [], True
    used_rules, fired_rules = [], []
    for t, i1, i2, j1, j2 in xops:
        a = pmap.get(i1) if i1 < len(xoc) else len(yoc)
        if a is None:
            # 锚点块被规则消除（HEAD 上无 Y 对应块），无法配对 → 疑似
            pairs.append(("suspect",
                          ("\n".join(xoc[i1:i2]), "\n".join(xnc[j1:j2])), None))
            gate = False
            continue
        key = (a, t, i2 - i1, j2 - j1)
        xo_s, xn_s = "\n".join(xoc[i1:i2]), "\n".join(xnc[j1:j2])
        if key not in y_by_key:
            # X 侧独有组：1:1 逐块判定规则采纳；结构组 → 疑似
            gate = False
            if t == "replace" and (i2 - i1) == (j2 - j1):
                for k in range(i2 - i1):
                    xo, xn = xoc[i1 + k], xnc[j1 + k]
                    used: list = []
                    kind = xonly_kind(vol, xo, xn, used)
                    used_rules += used
                    pairs.append((kind, (xo, xn), None))
            else:
                pairs.append(("suspect", (xo_s, xn_s), None))
            continue
        _, yi1, yi2, yj1, yj2 = y_by_key[key].pop(0)
        yo_s, yn_s = "\n".join(yoc[yi1:yi2]), "\n".join(ync[yj1:yj2])
        if apply_rules(vol, xo_s) != yo_s or apply_rules(vol, xn_s) != yn_s:
            pairs.append(("suspect", (xo_s, xn_s), (yo_s, yn_s)))
            gate = False
            continue
        if t != "replace" or (i2 - i1) != (j2 - j1):
            pairs.append(("sync", (xo_s, xn_s), (yo_s, yn_s)))  # 结构组
            continue
        for k in range(i2 - i1):  # 1:1 组逐块走块级分类
            kind, used, fired = classify_block(vol, rules,
                                               xoc[i1 + k], xnc[j1 + k],
                                               yoc[yi1 + k], ync[yj1 + k])
            used_rules += used
            fired_rules += fired
            pairs.append((kind, (xoc[i1 + k], xnc[j1 + k]),
                          (yoc[yi1 + k], ync[yj1 + k])))
            if kind not in ("sync", "rulekilled"):
                gate = False
    for ops in y_by_key.values():  # Y 侧未配对组 → 疑似
        for t, yi1, yi2, yj1, yj2 in ops:
            pairs.append(("suspect", None,
                          ("\n".join(yoc[yi1:yi2]), "\n".join(ync[yj1:yj2]))))
            gate = False
    return pairs, gate, used_rules, fired_rules


def revert_block_positional(text: str, head_bytes: bytes,
                            o: str, n: str) -> str | None:
    """按块序定位还原：在 HEAD→text 的 1:1 replace 中找到 norm 级匹配 (o,n)
    的唯一块，按其在新文本块序列中的位置顺序扫描定位 raw 形态并替换回 o。

    用于文本过短（如「。」」）无法按字符串唯一性定位的排除块。
    匹配不唯一或定位失败返回 None。
    """
    oc, nc = text_chunks(head_bytes), text_chunks(text.encode("utf-8"))
    on, nn = [norm_ws(c) for c in oc], [norm_ws(c) for c in nc]
    sm = difflib.SequenceMatcher(a=on, b=nn, autojunk=False)
    targets = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for oi, nj in zip(range(i1, i2), range(j1, j2)):
            if on[oi] == o and nn[nj] == n:
                targets.append(nj)
    if len(targets) != 1:
        return None
    nj = targets[0]
    pos = 0
    for idx, chunk in enumerate(nc):
        cands = [(text.find(v, pos), k)
                 for k, v in enumerate(raw_variants(chunk))
                 if v and text.find(v, pos) >= 0]
        if not cands:
            return None
        p, k = min(cands)
        v = raw_variants(chunk)[k]
        if idx == nj:
            ovs = raw_variants(o)
            return text[:p] + ovs[min(k, len(ovs) - 1)] + text[p + len(v):]
        pos = p + len(v)
    return None


def xonly_kind(vol: str, xo: str, xn: str, used: list) -> str:
    """仅 X 有改动的块的分类：片段收敛 → 规则采纳；规则管道等价
    （apply_rules 后新旧文本一致，即上游改动整个落在规则覆盖范围内，
    fixed 后 Y 不受影响）也是规则采纳——覆盖多步推导（如 `….` 先去点
    再双写省略号得到 `……`）这类片段级收敛看不见的情形。其余 → 疑似。"""
    if convergent(vol, xo, xn, set(), used):
        return "adopted"
    if apply_rules(vol, xo) == apply_rules(vol, xn):
        return "adopted"
    return "suspect"


def convergent(vol: str, o: str, n: str, other_frags: set, used: list) -> bool:
    """X 侧块 (o,n) 相对 other_frags 的 X 独有改动是否全部由 x2y 规则解释。

    反复应用「能严格缩小 (o,n) 差异片段集」的规则；收敛后剩余片段都在
    other_frags 中，则该块的 X 独有部分是规则覆盖的（上游采纳了规则）。
    被用到的规则记入 used，元素为 (section, old, new)——section 是 fixes
    的键（卷名或 "*"），供 s6b 定位并验证删除冗余规则。
    """
    cur = block_frags(o, n)
    rules = ([(vol, ro, rn) for ro, rn in fixes.get(vol, [])]
             + [("*", ro, rn) for ro, rn in fixes["*"]])
    changed = True
    while changed:
        changed = False
        for sec, ro, rn in rules:
            o2 = re.sub(ro, rn, o)
            if o2 == o:
                continue
            f2 = block_frags(o2, n)
            if f2 < cur:
                used.append((sec, ro, rn))
                o, cur, changed = o2, f2, True
    return cur <= other_frags


def classify() -> dict:
    """对当前工作区改动做逐文件块对分类（s5/s6a/s6b 共用）。

    返回 {pairs_by_rel, complex, new_files, rule_use, touched_rules, head, work}：
    - pairs_by_rel[rel] = [(kind, xblock_or_None, yblock_or_None)]，
      kind: "sync" / "mixed" / "adopted" / "rulekilled" / "suspect"
    - rule_use: {(section, ro, rn): {rel, ...}}，收敛判定用到的规则
    - touched_rules: 同构，能命中改动块旧 X 文本的全部规则（s6b 候选超集）
    - rulekilled_rules: 同构，规则失效型收敛块旧文本命中的规则
    - head/work: 路径 -> bytes（单侧无改动的另一侧以 HEAD 内容充当）
    """
    git("add", "-A")
    entries = git("status", "--porcelain", "-z").decode("utf-8").split("\0")
    mfiles = [e[3:] for e in entries
              if e and e[0] == "M" and not e.startswith("R")
              and e[3:].lower().endswith(TEXT_EXT)]

    hm = head_sha_map()
    cf = CatFile()
    head: dict[str, bytes] = {}
    work: dict[str, bytes] = {}
    new_files = set()
    for path in mfiles:
        if path not in hm:
            new_files.add(path)
            continue
        head[path] = cf.read(hm[path])
        work[path] = open(path, "rb").read()
    cf.close()

    groups = defaultdict(dict)
    for p in mfiles:
        root, rel = p.split("/", 1)
        groups[rel][root] = p

    pairs_by_rel: dict[str, list] = {}
    complex_rels, new_only = [], []
    structural_ok: set = set()
    rule_use: dict[tuple, set] = defaultdict(set)
    touched_rules: dict[tuple, set] = defaultdict(set)
    rulekilled_rules: dict[tuple, set] = defaultdict(set)
    for rel, d in sorted(groups.items()):
        xp, yp = d.get("X"), d.get("Y")
        if (xp and xp in new_files) or (yp and yp in new_files):
            new_only.append(rel)
            continue
        if not xp and not yp:
            continue
        # 单侧无改动：该侧取 HEAD 内容充当 head=work（空改动集），照常配对；
        # 该侧在 HEAD 也不存在（增删）→ 复杂，人工
        missing = False
        for side, p in (("X", xp), ("Y", yp)):
            if p is None:
                q = f"{side}/{rel}"
                if q not in hm:
                    missing = True
                    break
                head[q] = git("show", f"HEAD:{q}")
                work[q] = head[q]
                d[side] = q
        if missing:
            complex_rels.append(rel)
            continue
        xp, yp = d["X"], d["Y"]
        xch = indexed_changes(head[xp], work[xp])
        ych = indexed_changes(head[yp], work[yp])
        structural = xch is None or ych is None
        if not structural and not xch and not ych:
            continue
        pmap = positional_map(head[xp], head[yp], vol=rel.split("/")[0])
        if pmap is None:
            complex_rels.append(rel)
            continue
        vol = rel.split("/")[0]
        # 规则触点：规则若能命中改动块的【旧】X 文本，上游改动可能使其失效
        # （收敛采纳 / 改写为第三种形式都算）——全部记为 s6b 候选，验证把关
        rules = ([(vol, ro, rn) for ro, rn in fixes.get(vol, [])]
                 + [("*", ro, rn) for ro, rn in fixes["*"]])
        if structural:
            # 结构改动（块增删/N→M）：块组级配对分类。gate 通过（全
            # sync/rulekilled）的文件记入 structural_ok，--commit 时整文件
            # 成对提交；否则整文件留审（块对照常导出供审查/反馈）
            pairs, gate, used, fired = structural_pairs(
                vol, rules, head[xp], work[xp], head[yp], work[yp], pmap)
            for r in used:
                rule_use[r].add(rel)
            for r in fired:
                rulekilled_rules[r].add(rel)
            for _, xb, _ in pairs:
                if xb:
                    for r in rules:
                        if re.search(r[1], xb[0]):
                            touched_rules[r].add(rel)
            pairs_by_rel[rel] = pairs
            if gate and pairs:
                structural_ok.add(rel)
            continue
        for _, xo, _ in xch:
            for r in rules:
                if re.search(r[1], xo):
                    touched_rules[r].add(rel)
        y_by_oi = {oi: (o, n) for oi, o, n in ych}
        matched_yi = set()
        pairs = []
        for oi, xo, xn in xch:
            yb = y_by_oi.get(pmap.get(oi))
            if yb is None:
                used: list = []
                kind = xonly_kind(vol, xo, xn, used)
                for r in used:
                    rule_use[r].add(rel)
                pairs.append((kind, (xo, xn), None))
                continue
            matched_yi.add(pmap.get(oi))
            yo, yn = yb
            kind, used, fired = classify_block(vol, rules, xo, xn, yo, yn)
            for r in used:
                rule_use[r].add(rel)
            for r in fired:
                rulekilled_rules[r].add(rel)
            pairs.append((kind, (xo, xn), yb))
        for oi, yo, yn in ych:
            if oi not in matched_yi:
                pairs.append(("suspect", None, (yo, yn)))  # 仅 Y 有改动
        pairs_by_rel[rel] = pairs

    return {"pairs_by_rel": pairs_by_rel, "complex": complex_rels,
            "new_files": new_only, "rule_use": rule_use,
            "touched_rules": touched_rules,
            "rulekilled_rules": rulekilled_rules,
            "structural_ok": structural_ok,
            "head": head, "work": work}


def main() -> None:
    do_commit = "--commit" in sys.argv
    r = classify()
    pairs_by_rel = r["pairs_by_rel"]
    complex_rels, new_only = r["complex"], r["new_files"]
    rule_use = r["rule_use"]
    structural_ok = r["structural_ok"]
    head, work = r["head"], r["work"]

    n_sync = sum(1 for ps in pairs_by_rel.values() for k, _, _ in ps if k == "sync")
    n_mixed = sum(1 for ps in pairs_by_rel.values() for k, _, _ in ps if k == "mixed")
    n_adopted = sum(1 for ps in pairs_by_rel.values() for k, _, _ in ps if k == "adopted")
    n_rk = sum(1 for ps in pairs_by_rel.values() for k, _, _ in ps if k == "rulekilled")
    n_susp = sum(1 for ps in pairs_by_rel.values() for k, _, _ in ps if k == "suspect")
    print(f"{len(pairs_by_rel):5d}  有文本改动的文件对")
    print(f"{n_sync:5d}  正常同步候选块对（待人工审查）")
    print(f"{n_mixed:5d}  混合块对（片段级拆分：正常同步片段两侧提交）")
    print(f"{n_adopted + n_mixed:5d}  规则采纳块（上游采纳既有规则，留 s6a）")
    print(f"{n_rk:5d}  规则失效型收敛块（两侧收敛，留 s6a）")
    print(f"{n_susp:5d}  疑似上游错误块对（留下）")
    print(f"{len(complex_rels):5d}  复杂（对齐失败，留下）")
    if structural_ok:
        print(f"{len(structural_ok):5d}  结构改动文件（块组已配对收敛，"
              f"全过审后整文件成对提交）")
    if new_only:
        print(f"{len(new_only):5d}  新增文件（未处理，人工）")

    # 导出审查材料
    os.makedirs(STATE_DIR, exist_ok=True)
    agg = Counter()
    for ps in pairs_by_rel.values():
        for k, xb, _ in ps:
            if k in ("sync", "mixed") and xb:
                agg[xb] += 1
    with open(os.path.join(STATE_DIR, "review_changes.txt"), "w",
              encoding="utf-8") as f:
        for (o, n), c in agg.most_common():
            f.write(f"[{c}次]\n- {o}\n+ {n}\n\n")
    with open(os.path.join(STATE_DIR, "suspect_changes.txt"), "w",
              encoding="utf-8") as f:
        for rel, ps in pairs_by_rel.items():
            for k, xb, yb in ps:
                if k != "suspect":
                    continue
                for side, b in (("X", xb), ("Y", yb)):
                    if b:
                        f.write(f"[{side}] {rel}\n- {b[0]}\n+ {b[1]}\n\n")
    plan = {
        "summary": {"pairs": len(pairs_by_rel), "sync": n_sync,
                    "mixed": n_mixed, "adopted": n_adopted,
                    "rulekilled": n_rk, "suspect": n_susp,
                    "complex": len(complex_rels)},
        "files": {rel: [{"kind": k, "x": xb, "y": yb}
                        for k, xb, yb in ps if k != "sync"]
                  for rel, ps in pairs_by_rel.items()
                  if any(k != "sync" for k, _, _ in ps)},
        "complex": complex_rels,
        "structural_ok": sorted(structural_ok),
        "new_files": new_only,
        "adopted_rules": {f"{sec}: {ro} -> {rn}": sorted(rels)
                       for (sec, ro, rn), rels in sorted(rule_use.items())},
    }
    json.dump(plan, open(os.path.join(STATE_DIR, "plan.json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"审查材料: {os.path.join(STATE_DIR, 'review_changes.txt')}"
          f"（{sum(agg.values())} 块 / 去重 {len(agg)}）")
    if n_susp:
        print(f"疑似上游错误: {os.path.join(STATE_DIR, 'suspect_changes.txt')}")

    if not do_commit:
        print("审查后把可疑改动（整块 old/new）写入 "
              f"{os.path.join(STATE_DIR, 'triage_exclude.json')}，再带 --commit 运行")
        return

    excl_path = os.path.join(STATE_DIR, "triage_exclude.json")
    excludes = set()
    if os.path.exists(excl_path):
        excludes = {tuple(x) for x in json.load(open(excl_path, encoding="utf-8"))}
        print(f"排除规则 {len(excludes)} 条（按块对生效）")

    fails = 0
    committed_rels = 0
    for rel, ps in pairs_by_rel.items():
        if rel in structural_ok:
            # 结构文件：结构组（增删/N→M）无法按块还原，其余按块对提交——
            # 被排除的 1:1 块在两侧工作区文本上还原回 HEAD 旧文本后提交中间
            # 版本（与纯 1:1 文件的排除语义一致：错误块留在工作区等上游修复）
            ex_pairs = [(xb, yb) for _, xb, yb in ps
                        if (xb and xb in excludes) or (yb and yb in excludes)]
            if any(xb is None or yb is None
                   or not xb[0] or not xb[1] or not yb[0] or not yb[1]
                   or "\n" in xb[0] or "\n" in xb[1]
                   for xb, yb in ex_pairs):
                print("  结构文件的结构组命中排除（无法按块还原），整文件留审:", rel)
                fails += 1
                continue
            xp, yp = "X/" + rel, "Y/" + rel
            vol = rel.split("/")[0]
            inter, ok = {}, True
            for p, side in ((xp, 0), (yp, 1)):
                text = work[p].decode("utf-8")
                # 同一文本的排除块可能多处出现：出现次数与排除次数相等时
                # 说明所有出现都是被排除块，可安全地全部还原
                groups = Counter((xb, yb)[side] for xb, yb in ex_pairs)
                for (o, n), m in groups.items():
                    nvs, ovs = raw_variants(n), raw_variants(o)
                    hit = next((k for k, v in enumerate(nvs)
                                if v and text.count(v) == m), None)
                    if hit is not None:
                        text = text.replace(nvs[hit], ovs[hit])
                        continue
                    # 文本过短无法按唯一性定位：按块序定位（排除块在
                    # HEAD→中间版的 1:1 replace 中须唯一）
                    r = revert_block_positional(text, head[p], o, n)
                    if r is None:
                        ok = False
                        break
                    text = r
                if not ok:
                    break
                inter[p] = text
            if not ok:
                print("  被排除块定位歧义，整文件留审:", rel)
                fails += 1
                continue
            # 校验 1：还原后两侧块多重集差异恰好是被排除对
            for p, side in ((xp, 0), (yp, 1)):
                wc = Counter(norm_ws(c) for c in text_chunks(work[p]))
                ic = Counter(norm_ws(c)
                             for c in text_chunks(inter[p].encode("utf-8")))
                want_o, want_n = Counter(), Counter()
                for xb, yb in ex_pairs:
                    b = (xb, yb)[side]
                    want_o[b[0]] += 1
                    want_n[b[1]] += 1
                if ic - wc != want_o or wc - ic != want_n:
                    ok = False
            if not ok:
                print("  中间版本校验失败，整文件留审:", rel)
                fails += 1
                continue
            # 校验 2：不变式（与 check_y_freshness 同语义：换行归一化后
            # fixed(X) == Y；x2y 写出的 Y 与上游 X 换行风格可能不同）
            xtxt = inter[xp].replace("\r\n", "\n")
            ytxt = inter[yp].replace("\r\n", "\n")
            if fixed(vol, xtxt) != ytxt:
                print("  fixed(X) != Y，不变式不满足，整文件留审:", rel)
                fails += 1
                continue
            excluded_xbs = {xb for xb, _ in ex_pairs}
            terms = [t for t in (group_summary(xb[0], xb[1])
                                 for _, xb, _ in ps
                                 if xb and xb not in excluded_xbs) if t]
            n_commit = len(ps) - len(ex_pairs)
            body = "\n".join(terms[:6]) + ("\n…" if len(terms) > 6 else "")
            paths = [p for p in (xp, yp) if head[p] != work[p]]
            saved = {}
            try:
                for p in paths:
                    saved[p] = open(p, "rb").read()
                    with open(p, "wb") as f:
                        f.write(inter[p].encode("utf-8"))
                commit_paths(f"fix: sync {rel}（{n_commit} 处文本修订）",
                             body, paths)
                committed_rels += 1
            except Exception as ex:
                fails += 1
                print("  FAIL:", rel, str(ex)[:150])
            finally:
                for p, b in saved.items():
                    with open(p, "wb") as f:
                        f.write(b)
            git("add", "-A")
            continue
        keepx, keepy, partx, partn = [], [], {}, {}
        for k, xb, yb in ps:
            if k not in ("sync", "mixed"):
                continue
            if (xb and xb in excludes) or (yb and yb in excludes):
                continue  # 块对级排除
            if k == "sync":
                keepx.append(xb)
                keepy.append(yb)
            else:  # 混合块对：Y 整块提交，X 侧片段级拆分只应用正常同步片段
                fy = block_frags(*yb)
                keepy.append(yb)
                partx[xb] = block_frags(*xb) - fy
                partn[xb] = (xb[0], apply_frags(xb[0], xb[1], fy))
        if not keepx and not keepy and not partx:
            continue
        xp, yp = "X/" + rel, "Y/" + rel
        inter = {}
        if keepx or partx:
            s = apply_blocks(head[xp], work[xp], set(keepx), partial=partx)
            if s is None:
                print("  定位歧义，整文件留审:", rel)
                fails += 1
                continue
            inter[xp] = s
        if keepy:
            s = apply_blocks(head[yp], work[yp], set(keepy))
            if s is None:
                print("  定位歧义，整文件留审:", rel)
                fails += 1
                continue
            inter[yp] = s
        # 校验：中间版本恰好 = keep 块 ∪ 混合块的部分应用（混合块允许
        # 因片段定位失败退化为整块还原，即不出现）
        ok, ncx, ncy = True, 0, 0
        for p, keep, partok, which in (
                (xp, keepx, set(partn.values()), "x"),
                (yp, keepy, set(), "y")):
            if p not in inter:
                continue
            got = chunk_changes(head[p], inter[p].encode("utf-8"))
            if got is None or not set(keep) <= set(got) <= set(keep) | partok:
                print("  中间版本校验失败，整文件留审:", rel)
                ok = False
            elif which == "x":
                ncx = len(got)
            else:
                ncy = len(got)
        if not ok:
            fails += 1
            continue
        paths = list(inter)
        if rel.lower().endswith((".opf", ".ncx")):
            subject, body = f"chore: sync {rel}", "元数据/时间戳更新"
        else:
            terms = [t for t in (term_summary(o, n) for o, n in keepx) if t]
            body = "\n".join(terms[:6]) + ("\n…" if len(terms) > 6 else "")
            subject = f"fix: sync {rel}（{ncx + ncy} 处文本修订）"
        saved = {}
        try:
            for p in paths:
                saved[p] = open(p, "rb").read()
                with open(p, "wb") as f:
                    f.write(inter[p].encode("utf-8"))
            commit_paths(subject, body, paths)
            committed_rels += 1
        except Exception as ex:
            fails += 1
            print("  FAIL:", rel, str(ex)[:150])
        finally:
            for p, b in saved.items():
                with open(p, "wb") as f:
                    f.write(b)
        git("add", "-A")
    git("add", "-A")
    left = [l for l in git("status", "--porcelain").decode("utf-8").splitlines() if l]
    print(f"完成。提交 {committed_rels} 对，失败/留审 {fails}，剩余暂存 {len(left)}")


if __name__ == "__main__":
    main()
