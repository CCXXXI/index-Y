"""把审定的候选规则写入分卷 TSV：归属验证 + fixed() 双模拟，全过才落盘。

输入候选规则 TSV（UTF-8，首行表头 `旧<TAB>新<TAB>注释`，与 rules/ 同格式）：
1. 字段卫生：无 tab/换行污染、old≠new、候选内不重复、不与既有分卷规则撞 old
   （取代既有规则用 --replace <旧串>，可重复）；
2. 正则守卫：rules/ 的旧串是 regex（x2y 用 re.sub 应用）——逐条编译，且全语料上
   字面计数与正则计数必须相等，并以哨兵比较正则替换与逐字替换结果；
3. 归属验证：每条 old 的全语料命中须全部落在本卷（入分卷段）；有跨卷命中即失败
   并打印各条计数，由人裁决（加长旧串保分卷，或人工评估后另行入 _common.tsv）；
4. 管道模拟：按 x2y 顺序（分卷先、通用后，文件内自上而下）对本卷 X 语料跑替换，
   断言每条规则的触发次数等于其本卷命中数、终态（新串再过通用规则）出现；
5. 在临时 rules 树重新加载真实 x2y 模块再验一遍（真实规则校验 + 真管道终验），
   全过才原子替换目标 TSV（保留表头与 # 行、删 --replace 行、追加候选）。

落盘后：重跑 `uv run python scripts/sync/x2y.py`，再用 --verify-y 对 Y 产物终验，
rules/ 与 EPUB/ 同一 commit 提交。

用法：
  uv run python scripts/agent_proofread/apply_rules.py <卷> candidates.tsv [--replace 旧串]...
  uv run python scripts/agent_proofread/apply_rules.py <卷> candidates.tsv --verify-y
"""

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import regex as re
from tqdm import tqdm

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import jp_align

RULES_DIR = ROOT / "rules"
TEXT_EXT = (".xhtml", ".opf", ".ncx")
HEADER = "旧\t新\t注释"


def parse_tsv(path: Path) -> list[tuple[str, str, str]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0] != HEADER:
        sys.exit(f"{path}: 首行须为表头「{HEADER}」")
    out = []
    for lineno, raw in enumerate(lines[1:], 2):
        fields = raw.split("\t")
        if len(fields) != 3:
            sys.exit(f"{path}:{lineno}: 字段数 {len(fields)} ≠ 3")
        out.append((fields[0], fields[1], fields[2]))
    return out


def resolve_vol(vol_arg: str) -> str:
    return jp_align.resolve_vol(vol_arg, ROOT / "index-X" / "EPUB")


def corpus_files():
    for vol_dir in sorted((ROOT / "index-X" / "EPUB").iterdir()):
        if not vol_dir.is_dir():
            continue
        for p in sorted(vol_dir.rglob("*")):
            if p.is_file() and p.suffix in TEXT_EXT:
                yield vol_dir.name, p


def final_form(new: str, common: list[tuple[str, str]]) -> str:
    for old, new_ in common:
        new = re.sub(old, new_, new)
    return new


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("vol", help="卷目录名或卷码（如 S1_02）")
    ap.add_argument("candidates", type=Path, help="候选规则 TSV（旧/新/注释）")
    ap.add_argument(
        "--replace",
        action="append",
        default=[],
        help="被候选取代、需从分卷 TSV 删除的既有旧串（可重复）",
    )
    ap.add_argument(
        "--verify-y",
        action="store_true",
        help="不写盘：对 Y 产物终验（x2y 重建后运行）",
    )
    args = ap.parse_args()

    vol = resolve_vol(args.vol)
    tsv = RULES_DIR / f"{vol}.tsv"
    common = [
        (o, n)
        for o, n, _ in parse_tsv(RULES_DIR / "_common.tsv")
        if not o.startswith("#")
    ]
    candidates = parse_tsv(args.candidates)

    if args.verify_y:
        spec = importlib.util.spec_from_file_location(
            "x2y_verify_y", ROOT / "scripts/sync/x2y.py"
        )
        assert spec is not None and spec.loader is not None, "x2y.py 加载失败"
        x2y_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(x2y_mod)
        fixes = x2y_mod.__dict__["load_rules"]()
        x2y_mod.__dict__["fixes"] = fixes
        from sync.lib_triage import freshness_errors

        stale = freshness_errors(str(ROOT), x2y_mod.__dict__["fixed"])
        if stale:
            details = "\n".join(f"  {why}: {rel}" for rel, why in stale[:20])
            sys.exit(
                f"Y 终验失败：{len(stale)} 个文件与当前 X/rules 不一致：\n"
                + details
                + "\n请先运行: uv run python scripts/sync/x2y.py"
            )
        active = fixes.get(vol, [])
        missing = [(old, new) for old, new, _ in candidates if (old, new) not in active]
        if missing:
            details = "\n".join(f"  {old}→{new}" for old, new in missing)
            sys.exit("Y 终验失败：候选未进入活动分卷规则：\n" + details)
        print(f"Y 终验通过：全树与当前 X/rules 一致（{len(candidates)} 条候选）")
        return 0

    # 1. 字段卫生
    seen: set[str] = set()
    for old, new, note in candidates:
        if not old:
            sys.exit("存在空 old")
        if old == new:
            sys.exit(f"old==new: {old[:40]}")
        if old in seen:
            sys.exit(f"候选内 old 重复: {old[:40]}")
        seen.add(old)
        try:
            re.compile(old)
        except re.error as e:
            sys.exit(f"正则编译失败 {old[:40]}: {e}")

    existing_rows = parse_tsv(tsv) if tsv.exists() else []
    existing = [(o, n, c) for o, n, c in existing_rows if not o.startswith("#")]
    replace_set = set(args.replace)
    missing_replace = replace_set - {o for o, _, _ in existing}
    if missing_replace:
        sys.exit(f"--replace 的串不在既有分卷规则中: {missing_replace}")
    kept = [(o, n, c) for o, n, c in existing_rows if o not in replace_set]
    active_kept = [(o, n, c) for o, n, c in kept if not o.startswith("#")]
    clash = seen & {o for o, _, _ in active_kept}
    if clash:
        sys.exit(f"候选与既有分卷规则撞 old（取代请加 --replace）: {clash}")

    # 2+3. 全语料计数（字面与正则双口径）与归属
    total_lit = {o: 0 for o, _, _ in candidates}
    total_re = {o: 0 for o, _, _ in candidates}
    inv_lit = {o: 0 for o, _, _ in candidates}
    semantic_mismatch = set()
    sentinel = "\ue000INDEX_Y_LITERAL_SENTINEL\ue001"
    for v, p in tqdm(list(corpus_files()), desc="语料扫描"):
        text = p.read_text(encoding="utf-8")
        for o, _, _ in candidates:
            lit = text.count(o)
            if lit:
                total_lit[o] += lit
                if v == vol:
                    inv_lit[o] += lit
            regex_text, count = re.subn(o, sentinel, text)
            total_re[o] += count
            if regex_text != text.replace(o, sentinel):
                semantic_mismatch.add(o)
    bad = [
        f"{o[:40]}: 字面{total_lit[o]} 正则{total_re[o]}"
        for o, _, _ in candidates
        if total_lit[o] != total_re[o] or o in semantic_mismatch
    ]
    if bad:
        sys.exit("正则守卫失败（含未转义元字符？）：\n" + "\n".join(bad))
    out_of_vol = [
        f"{o[:40]}: 全语料{total_lit[o]} 本卷{inv_lit[o]}"
        for o, _, _ in candidates
        if total_lit[o] != inv_lit[o] or inv_lit[o] == 0
    ]
    if out_of_vol:
        sys.exit("归属待裁决（跨卷命中或本卷无命中）：\n" + "\n".join(out_of_vol))

    # 4. 管道模拟（与 x2y 同序）
    vol_rules = [(o, n) for o, n, _ in active_kept] + [(o, n) for o, n, _ in candidates]
    fired = {o: 0 for o, _, _ in candidates}
    transformed = []
    for p in sorted((ROOT / "index-X" / "EPUB" / vol).rglob("*")):
        if not (p.is_file() and p.suffix in TEXT_EXT):
            continue
        content = p.read_text(encoding="utf-8")
        for o, n in vol_rules:
            if o in fired:
                fired[o] += content.count(o)
            content = re.sub(o, n, content)
        for o, n in common:
            content = re.sub(o, n, content)
        transformed.append(content)
    sim_text = "\n".join(transformed)
    fails = [
        f"{o[:40]}: 触发{fired[o]} ≠ 本卷{inv_lit[o]}"
        for o, _, _ in candidates
        if fired[o] != inv_lit[o]
    ]
    fails += [
        f"终态未出现: {ff[:40]}"
        for _, n, _ in candidates
        if (ff := final_form(n, common)) and ff not in sim_text
    ]
    if fails:
        sys.exit("管道模拟失败：\n" + "\n".join(fails))

    # 5. 在临时 rules 树用真实 x2y 模块终验；全部通过后才原子替换目标。
    rendered = (
        "\n".join([HEADER] + [f"{o}\t{n}\t{c}" for o, n, c in kept + candidates]) + "\n"
    )
    with tempfile.TemporaryDirectory(prefix=".apply-rules-", dir=RULES_DIR) as tmp:
        temp_rules = Path(tmp)
        for rule_file in RULES_DIR.glob("*.tsv"):
            shutil.copy2(rule_file, temp_rules / rule_file.name)
        (temp_rules / tsv.name).write_text(rendered, encoding="utf-8", newline="\n")
        spec = importlib.util.spec_from_file_location(
            "x2y_mod", ROOT / "scripts/sync/x2y.py"
        )
        assert spec is not None and spec.loader is not None, "x2y.py 加载失败"
        x2y_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(x2y_mod)
        x2y_mod.__dict__["RULES_DIR"] = temp_rules
        x2y_mod.__dict__["fixes"] = x2y_mod.__dict__["load_rules"]()
        fixed = x2y_mod.__dict__["fixed"]
        real_text = "\n".join(
            fixed(vol, p.read_text(encoding="utf-8"))
            for p in sorted((ROOT / "index-X" / "EPUB" / vol).rglob("*"))
            if p.is_file() and p.suffix in TEXT_EXT
        )
        fails = [
            f"真管道终态未出现: {ff[:40]}"
            for _, n, _ in candidates
            if (ff := final_form(n, common)) and ff not in real_text
        ]
        if fails:
            sys.exit("真管道终验失败：\n" + "\n".join(fails))

    temp_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{tsv.name}.",
            suffix=".tmp",
            dir=RULES_DIR,
            delete=False,
        ) as f:
            temp_output = Path(f.name)
            f.write(rendered)
        os.replace(temp_output, tsv)
    finally:
        if temp_output is not None:
            temp_output.unlink(missing_ok=True)

    print(
        f"OK：{len(candidates)} 条写入 {tsv.name}"
        f"（删除被取代 {len(replace_set)} 条），双模拟全过。"
    )
    print(
        "下一步：uv run python scripts/sync/x2y.py && "
        f"uv run python {Path(__file__).relative_to(ROOT)} {vol} {args.candidates} --verify-y"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
