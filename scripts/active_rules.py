"""报告指定范围内当前生效的 x2y 规则及其首次生效点。

失活规则保留在 rules/ 中不删除（见 s6b），因此 rules/ 不再等同于
「当前生效的规则清单」；向上游反馈某卷/某章的修订时用本脚本实测。

「生效」定义与 s6b_report_inactive_rules.py 一致：把该规则从管道
（分卷段先于通用段，段内自上而下）中移除后，范围内某文本文件的最终
输出发生改变；仅命中但被后续规则再收敛的不算生效。扫描对象为工作区 X
中的 .xhtml/.opf/.ncx（与 x2y.py 的 TEXT_EXT 一致）。
"""

import argparse
import sys
from pathlib import Path

import regex as re
from tqdm import tqdm

ROOT = Path(__file__).parent.parent
RULES_DIR = ROOT / "rules"
X_DIR = ROOT / "X"
TEXT_EXT = (".xhtml", ".opf", ".ncx")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_rules() -> list[dict]:
    """解析 rules/*.tsv 为规则记录（含出处行号）；校验口径同 x2y.load_rules。"""
    x_vols = {p.name for p in X_DIR.iterdir() if p.is_dir()}
    rules = []
    errors = []
    for tsv in sorted(RULES_DIR.glob("*.tsv")):
        section = "*" if tsv.stem == "_common" else tsv.stem
        if section != "*" and section not in x_vols:
            errors.append(f"{tsv.name}: 文件名与 X/ 下卷目录不对应")
        seen = {}
        for lineno, raw in enumerate(
                tsv.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not raw or raw.startswith("#"):
                continue
            fields = raw.split("\t")
            if len(fields) != 2:
                errors.append(f"{tsv.name}:{lineno}: 字段数 {len(fields)} ≠ 2")
                continue
            old, new = fields
            if old in seen:
                errors.append(f"{tsv.name}:{lineno}: old 与第 {seen[old]} 行重复")
            seen[old] = lineno
            try:
                re.compile(old)
            except re.error as e:
                errors.append(f"{tsv.name}:{lineno}: 正则编译失败: {e}")
                continue
            rules.append({"section": section, "lineno": lineno,
                          "old": old, "new": new})
    if errors:
        sys.exit("rules/ 校验失败：\n" + "\n".join(errors))
    return rules


def pick_volume(arg: str) -> str:
    vols = sorted(p.name for p in X_DIR.iterdir() if p.is_dir())
    if arg in vols:
        return arg
    hits = [v for v in vols if arg in v]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit(f"X/ 下没有匹配「{arg}」的卷目录")
    sys.exit(f"「{arg}」匹配到多个卷目录：\n" + "\n".join(hits))


def pick_files(vol: str, chap: str | None) -> list[Path]:
    base = X_DIR / vol
    files = sorted(p for p in base.rglob("*")
                   if p.is_file() and p.suffix in TEXT_EXT)
    if chap is None:
        return files
    hits = [p for p in files if chap in p.relative_to(base).as_posix()]
    if not hits:
        sys.exit(f"{vol} 内没有匹配「{chap}」的文本文件")
    return hits


def first_point(vol: str, fidx: int, path: Path,
                original: str, pre: str, m) -> dict:
    """定位首个匹配：优先回溯到 X 原文位置；匹配文本本身是前置规则的产物时
    （在原文中找不到），退化为规则应用时文本中的位置并加注。"""
    rel = path.relative_to(X_DIR / vol).as_posix()
    matched = m.group(0)
    idx = original.find(matched) if matched else -1
    if idx >= 0:
        base, s, note = original, idx, ""
    else:
        base, s, note = pre, m.start(), "（位置为规则应用时文本，已被前置规则改写）"
    e = s + len(matched)
    line = base.count("\n", 0, s) + 1
    w = 30
    a, b = max(0, s - w), min(len(base), e + w)
    snip = re.sub(r"\s+", " ", base[a:s] + "【" + matched + "】" + base[e:b])
    return {"fidx": fidx, "rel": rel, "line": line,
            "snip": "…" + snip.strip() + "…", "note": note}


def scan(vol: str, files: list[Path], seq: list[dict]) -> dict:
    """逐文件跑完整管道，返回 {seq 下标: 首次生效点}（仅生效规则）。"""
    active = {}
    for fidx, path in enumerate(tqdm(files, "scan")):
        original = path.read_text(encoding="utf-8")
        content, pending = original, {}
        for i, r in enumerate(seq):
            out = re.sub(r["old"], r["new"], content)
            if out != content and i not in active:
                pending[i] = content  # 触发；记录应用前状态待严格判定
            content = out
        for i, pre in pending.items():  # 移除该规则后最终输出是否改变
            out = pre
            for r in seq[i + 1:]:
                out = re.sub(r["old"], r["new"], out)
            if out != content:
                active[i] = first_point(vol, fidx, path, original, pre,
                                        re.search(seq[i]["old"], pre))
    return active


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("volume", metavar="卷",
                        help="X/ 下卷目录名或其唯一子串（如 S4_01）")
    parser.add_argument("chapter", metavar="章节", nargs="?",
                        help="卷内文本文件相对路径的子串（如 Chapter1 或 "
                             "S4_01-04）；匹配到多个文件时全部纳入范围")
    args = parser.parse_args()
    vol = pick_volume(args.volume)
    files = pick_files(vol, args.chapter)
    rules = load_rules()
    seq = ([r for r in rules if r["section"] == vol]
           + [r for r in rules if r["section"] == "*"])
    active = scan(vol, files, seq)

    head = f"范围：X/{vol}"
    if args.chapter is not None:
        head += f" 内匹配「{args.chapter}」的 {len(files)} 个文本文件"
    else:
        head += f"（{len(files)} 个文本文件）"
    print(head)
    if not active:
        print("范围内无生效规则。")
        return 0
    ordered = sorted(active, key=lambda i: (active[i]["fidx"],
                                            active[i]["line"], i))
    n_vol = sum(1 for i in ordered if seq[i]["section"] == vol)
    print(f"生效规则 {len(ordered)} 条"
          f"（分卷段 {n_vol} · 通用段 {len(ordered) - n_vol}），"
          "按首次生效点排序：\n")
    for i in ordered:
        r, p = seq[i], active[i]
        sec = "分卷" if r["section"] == vol else "通用"
        new = r["new"] if r["new"] else "（删除）"
        print(f"[{sec}:{r['lineno']}] {r['old']} -> {new}")
        print(f"  首次生效 {p['rel']}:{p['line']}{p['note']}")
        print(f"  {p['snip']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
