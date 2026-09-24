"""阅读校对定位器：笔记关键词一次性输出三侧命中，供逐条判定与规则归属。

输入关键词清单（UTF-8 文本，一行一条；`#` 开头为注释行，空行忽略；
行内 TAB 分隔的后半为日文原文侧关键词，可省略）：

    数据数据
    细小随片	細かい破片

对每条关键词输出：

- X/ 全语料计数（按卷分组）——唯一命中入分卷段，多卷复现入 `_common.tsv`；
- 目标卷 X/ 与 Y/ 的命中上下文（保留 xhtml 标签原文，供规则旧串逐字截取）；
- 日文原文侧命中上下文（剥 `<rt>` 注音、去标签、归并空白后搜索，
  原文卷目录按 `[Sx_yy]` 前缀与 X/ 卷目录对应）。

用法：uv run python scripts/proofreading_locate.py "[S2_01]新约 某魔法的禁书目录 01X" needles.txt
卷参数可只给 `[Sx_yy]` 前缀；原文仓库默认 ../index-jp，用 --jp-root 覆盖。
"""

import argparse
import html
import re
import sys
from pathlib import Path
from typing import NamedTuple

from tqdm import tqdm

ROOT = Path(__file__).parent.parent
TEXT_EXT = (".xhtml", ".html", ".htm")
CONTEXT_CHARS = 60


class Needle(NamedTuple):
    cn: str
    jp: str | None


def parse_needles(path: Path) -> list[Needle]:
    needles = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cn, _, jp = line.partition("\t")
        needles.append(Needle(cn, jp.strip() or None))
    return needles


def resolve_vol(vol_arg: str, x_dir: Path) -> str:
    """卷参数支持全名或 `[Sx_yy]` 前缀，须唯一匹配 X/ 下卷目录。"""
    matches = [
        d.name for d in x_dir.iterdir() if d.is_dir() and d.name.startswith(vol_arg)
    ]
    if len(matches) != 1:
        sys.exit(f"卷参数 {vol_arg!r} 在 X/ 下匹配到 {len(matches)} 个目录: {matches}")
    return matches[0]


def strip_jp(raw: str) -> str:
    """原文搜索预处理：剥 rt 注音、去标签、归并空白（index-jp AGENTS.md 要求的对照姿势）。"""
    t = re.sub(r"<rt>.*?</rt>", "", raw, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", "", html.unescape(t))


def find_contexts(text: str, needle: str, width: int = CONTEXT_CHARS) -> list[str]:
    contexts = []
    start = 0
    while (i := text.find(needle, start)) >= 0:
        s, e = max(0, i - width), min(len(text), i + width)
        contexts.append(text[s:e].replace("\n", ""))
        start = i + 1
    return contexts


def corpus_counts(root: Path, needles: list[Needle]) -> dict[str, dict[str, int]]:
    """单遍扫描 root 下全语料，返回 {cn: {卷/文件: 次数}}（只保留有命中的条目）。"""
    counts: dict[str, dict[str, int]] = {n.cn: {} for n in needles}
    files = [f for f in root.rglob("*") if f.suffix in TEXT_EXT]
    for f in tqdm(files, desc="scan"):
        text = f.read_text(encoding="utf-8")
        vol = f.relative_to(root).parts[0]
        for n in needles:
            if c := text.count(n.cn):
                counts[n.cn][f"{vol}/{f.name}"] = c
    return counts


def vol_contexts(root: Path, vol: str, needle: str) -> list[str]:
    hits = []
    vol_dir = root / vol
    if not vol_dir.is_dir():
        return hits
    for f in sorted(vol_dir.rglob("*")):
        if f.suffix not in TEXT_EXT:
            continue
        text = f.read_text(encoding="utf-8")
        hits += [f"{f.name} :: {c}" for c in find_contexts(text, needle)]
    return hits


def jp_contexts(jp_root: Path, vol: str, needle: str) -> list[str]:
    prefix = vol.split("]")[0] + "]"
    matches = [d for d in jp_root.iterdir() if d.is_dir() and d.name.startswith(prefix)]
    if not matches:
        return [f"(../index-jp 无 {prefix} 对应目录）"]
    hits = []
    for f in sorted(matches[0].rglob("*")):
        if f.suffix not in TEXT_EXT:
            continue
        text = strip_jp(f.read_text(encoding="utf-8"))
        hits += [f"{f.name} :: {c}" for c in find_contexts(text, needle)]
    return hits


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("vol", help="目标卷（X/ 下卷目录全名或 [Sx_yy] 前缀）")
    p.add_argument("needles", type=Path, help="关键词清单文件（UTF-8）")
    p.add_argument("--jp-root", type=Path, default=ROOT.parent / "index-jp")
    p.add_argument(
        "--side",
        choices=["both", "y"],
        default="both",
        help="both=计数与上下文含 X 侧（默认，供规则归属与旧串截取）；"
        "y=只给 Y 侧（agent 通读校对用：子代理证据侧是 Y+日文原文，"
        "X/Y 差异只会诱导误报）",
    )
    args = p.parse_args()

    needles = parse_needles(args.needles)
    if not needles:
        sys.exit(f"{args.needles}: 关键词清单为空")
    vol = resolve_vol(args.vol, ROOT / "X")
    count_root = ROOT / "X" if args.side == "both" else ROOT / "Y"
    counts = corpus_counts(count_root, needles)

    for n in needles:
        print(f"===== {n.cn}")
        per_file = counts[n.cn]
        total = sum(per_file.values())
        if args.side == "both":
            placement = (
                "分卷段"
                if total and {k.split("/")[0] for k in per_file} == {vol}
                else "_common.tsv"
            )
            print(f"  [计数] 全语料 {total} 次 -> {placement}")
        else:
            print(f"  [计数] Y 全语料 {total} 次")
        for k, c in per_file.items():
            print(f"         {k} x{c}")
        sides = ("X", "Y") if args.side == "both" else ("Y",)
        for side in sides:
            for hit in vol_contexts(ROOT / side, vol, n.cn):
                print(f"  [{side}] {hit}")
        if n.jp:
            for hit in jp_contexts(args.jp_root, vol, n.jp):
                print(f"  [JP] {hit}")


if __name__ == "__main__":
    main()
