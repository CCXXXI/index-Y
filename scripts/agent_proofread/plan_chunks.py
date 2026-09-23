"""agent 通读校对的切块方案：把整卷正文切成 ~1.5 万字符的块，供并行分派。

输出每块的「文件 + 校对主区间 + 字符数」与可直接粘贴的 delegate_task goal 行，
末尾再印出全部块共用的 context 短指令与 output_schema——goal/context/output_schema
三样均以本脚本输出为准直接复制，主代理不得凭记忆抄写模板（长重复 JSON 抄写会塌缩）。
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

    # 全部块共用的分派参数：context 短指令（子 agent 自载模板）+ output_schema。
    # schema 单一来源是模板【output_schema】节，此处提取后原样转印。
    template = (ROOT / ".agents" / "skills" / "agent-proofread"
                / "templates" / "chapter-agent-prompt.md").read_text(encoding="utf-8")
    m = re.search(r"【output_schema】[^\n]*\n(\{.*?\})\s*$", template, flags=re.DOTALL)
    if not m:
        sys.exit("模板【output_schema】节提取失败，请检查模板尾部格式")
    schema = re.sub(r"\s+", "", m.group(1))
    vol_prefix = vol.split("]")[0]
    print("\ncontext（全部块共用，逐字复制进每个 task）:")
    print(f'按本仓库 .agents/skills/agent-proofread/templates/chapter-agent-prompt.md 模板执行'
          f'（先 skill_view(name="agent-proofread", file_path="templates/chapter-agent-prompt.md") 加载）：'
          f'模板中 {{{{VOL_PREFIX}}}} 替换为 {vol_prefix}（含左方括号）。'
          f'goal 给出的行区间即你的校对主区间，逐行通读；疑点回原文核对用模板规定的 proofreading_locate.py 批量清单法。'
          f'final answer 严格输出模板【产出】节规定的 JSON，不要多余文字。不修改任何文件。')
    print("\noutput_schema（全部块共用，逐字复制进每个 task）:")
    print(schema)
    return 0


if __name__ == "__main__":
    sys.exit(main())
