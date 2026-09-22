"""agent 通读校对的切块方案：把整卷正文切成 ~1.5 万字符的块，供并行分派。

输出每块的「文件 + 校对主区间 + 字符数」与可直接粘贴的 delegate_task goal 行。
切块规则：
- 只含正文文件（按文件名排序，内容序即顺序）；作品级包装页（Cover/Back_cover/
  Illustrations/Information/Introduction/Note/Special 等无内容序后缀）跳过；
- 小文件合并为一块（整读）；大文件按行界切块，主区间无缝拼接，块间以
  --overlap 行互作上下文（发现只报主区间，由提示词约束）；
- 总块数超过 --max-chunks 时自动加大目标字符数重切，直到 ≤ 上限。

用法：uv run python scripts/agent_proofread/plan_chunks.py <卷> [--target-chars 15000]
"""

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
WRAPPER_SUFFIXES = ("Cover", "Back_cover", "Illustrations", "Information",
                    "Introduction", "Note", "Special")


def strip_cn(line: str) -> str:
    t = re.sub(r"<rt>(.*?)</rt>", r"\1", line, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def resolve_text_dir(vol_arg: str) -> tuple[str, Path]:
    matches = [d.name for d in (ROOT / "Y").iterdir() if d.is_dir() and d.name.startswith(vol_arg)]
    if len(matches) != 1:
        sys.exit(f"卷参数 {vol_arg!r} 在 Y/ 下匹配到 {len(matches)} 个目录: {matches}")
    text_dir = ROOT / "Y" / matches[0] / "OEBPS" / "Text"
    if not text_dir.is_dir():
        sys.exit(f"正文目录不存在: {text_dir}")
    return matches[0], text_dir


def content_files(text_dir: Path) -> list[Path]:
    files = []
    for p in sorted(text_dir.glob("*.xhtml")):
        tail = p.stem.rsplit("-", 1)[-1]  # 作品级包装后缀（带内容序的文件不在此列）
        if tail in WRAPPER_SUFFIXES:
            continue
        files.append(p)
    if not files:
        sys.exit(f"{text_dir} 下没有正文文件")
    return files


class Part:
    """一个块内的一段：file 的 main_start..main_end 行（1 起始，含端点）。"""

    def __init__(self, path: Path, start: int, end: int, total: int) -> None:
        self.path, self.start, self.end, self.total = path, start, end, total

    @property
    def whole(self) -> bool:
        return self.start == 1 and self.end == self.total


class Chunk:
    def __init__(self) -> None:
        self.parts: list[Part] = []
        self.chars = 0

    def add(self, part: Part, chars: int) -> None:
        self.parts.append(part)
        self.chars += chars


def plan(files: list[Path], target: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur = Chunk()
    for path in files:
        sizes = [len(strip_cn(ln)) for ln in path.read_text(encoding="utf-8").split("\n")]
        total = len(sizes)
        lo = 0
        while lo < total:
            if cur.chars >= target:
                chunks.append(cur)
                cur = Chunk()
            hi, acc = lo, 0
            while hi < total and (acc + sizes[hi] <= target - cur.chars or hi == lo):
                acc += sizes[hi]
                hi += 1
            cur.add(Part(path, lo + 1, hi, total), acc)
            if hi < total:  # 大文件在此切断，主区间无缝，先收块
                chunks.append(cur)
                cur = Chunk()
            lo = hi
    if cur.parts:
        chunks.append(cur)
    return chunks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vol", help="卷目录名或 [Sx_yy] 前缀")
    ap.add_argument("--target-chars", type=int, default=15000, help="每块目标字符数")
    ap.add_argument("--overlap", type=int, default=10, help="上下文重叠行数")
    ap.add_argument("--max-chunks", type=int, default=10, help="最大并行块数")
    args = ap.parse_args()

    vol, text_dir = resolve_text_dir(args.vol)
    files = content_files(text_dir)
    target = args.target_chars
    while True:
        chunks = plan(files, target)
        if len(chunks) <= args.max_chunks or target > 40000:
            break
        target += 2000

    total_chars = sum(c.chars for c in chunks)
    print(f"{vol}: {len(files)} 个正文文件 -> {len(chunks)} 块"
          f"（目标 {target} 字符/块，共约 {total_chars // 1000}k 字符）")
    for i, c in enumerate(chunks, 1):
        segs = []
        for p in c.parts:
            seg = f"{p.path.name} L{p.start}-L{p.end}"
            if not p.whole:
                lo = max(1, p.start - args.overlap)
                hi = min(p.total, p.end + args.overlap)
                ctx = []
                if lo < p.start:
                    ctx.append(f"前 {lo}-{p.start - 1}")
                if hi > p.end:
                    ctx.append(f"后 {p.end + 1}-{hi}")
                seg += f"（上下文 {'，'.join(ctx)}）"
            segs.append(seg)
        print(f"块{i:2d} [{c.chars // 1000}k] {'、'.join(segs)}")
        goal = "；".join(
            f"{p.path} 第 {p.start}-{p.end} 行" + ("" if p.whole else "（前后各约 10 行为重叠上下文，发现只报主区间）")
            for p in c.parts)
        print(f"     goal: 校对通读 {goal}（约 {c.chars // 1000} 千字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
