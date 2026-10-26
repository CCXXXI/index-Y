"""agent 通读校对的切块方案：把整卷正文切块；行号对齐卷生成中日逐行对照文件，供并行分派。

bw_aligned pass 卷（Y 物理行号 ≡ 日文对齐单元行号，由 check_y_jp_alignment 门禁保证）：
每块生成一个对照文件（.triage/proofread_chunks/<卷码>/chunk-NN.txt，派生产物随轮重生成）——
每物理行一对「L<n> CN: …／L<n> JP: …」（CN=Y 侧剥标签、ruby 注音连写；JP=对齐单元同行
剥标签），双空行跳过，标题行加 H、重叠上下文行加 ctx 前缀；对齐缺失或行数不等的单元
该段只列 CN 并标注兜底。非 pass 卷维持旧形态（goal 直指 xhtml 行区间，locator 核对）。

输出每块的「文件 + 校对主区间 + 字符数」与可直接粘贴的 delegate_task goal 行，
末尾再印出全部块共用的 context 短指令与 output_schema——goal/context/output_schema
三样均以本脚本输出为准直接复制，主代理不得凭记忆抄写模板（长重复 JSON 抄写会塌缩）。
切块规则：
- 只含正文文件（按文件名排序，内容序即顺序）；作品级包装页（Cover/Back_cover/
  Illustrations/Information/Introduction/Note/Special 等无内容序后缀）跳过；
- 小文件合并为一块（整读）；大文件按行界切块，主区间无缝拼接，块间以
  --overlap 行互作上下文（发现只报主区间，由提示词约束）；
- 总块数超过 --max-chunks 时自动加大目标字符数重切；目标超过 40000 字符后停止，
  因此 --max-chunks 是软上限而非硬上限。

用法：uv run python scripts/agent_proofread/plan_chunks.py <卷> [--target-chars 12000]
"""

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import jp_align

WRAPPER_SUFFIXES = (
    "Cover",
    "Back_cover",
    "Illustrations",
    "Information",
    "Introduction",
    "Note",
    "Special",
)

CHUNK_DIR = ROOT / ".triage" / "proofread_chunks"


def strip_cn(line: str) -> str:
    t = re.sub(r"<rt>(.*?)</rt>", r"\1", line, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def resolve_text_dir(vol_arg: str) -> tuple[str, Path]:
    vol = jp_align.resolve_vol(vol_arg, ROOT / "EPUB")
    text_dir = ROOT / "EPUB" / vol / "OEBPS" / "Text"
    if not text_dir.is_dir():
        sys.exit(f"正文目录不存在: {text_dir}")
    return vol, text_dir


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
        sizes = [
            len(strip_cn(ln)) for ln in path.read_text(encoding="utf-8").splitlines()
        ]
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
    return [chunk for chunk in chunks if chunk.chars > 0]


def pair_lines(part: Part, aligned_dir: Path, overlap: int) -> list[str]:
    """一段的对照文本行：L<n> CN/JP 成对；无对齐时只列 CN 并在横幅标注兜底。"""
    # splitlines 与 jp_align.unit_stripped_lines 口径一致（split("\n") 在文件
    # 末尾换行时多出空元素，会误判行数不等）；迭代上限按实际行数收。
    y_raw = part.path.read_text(encoding="utf-8").splitlines()
    unit = jp_align.unit_code(part.path.name)
    jp = (
        jp_align.unit_stripped_lines(aligned_dir, unit)
        if (aligned_dir and unit)
        else None
    )
    paired = jp is not None and len(jp) == len(y_raw)
    banner = f"== {part.path.name} 主区间 L{part.start}-L{part.end}"
    banner += "（整文件）" if part.whole else "（ctx 行为重叠上下文，只读不报）"
    if not paired:
        banner += (
            " ※本单元无行号对齐（对齐缺失或行数不等），仅列 CN，日文核对用 locator 兜底"
        )
    out = [banner]
    lo = max(1, part.start - overlap)
    hi = min(part.total, part.end + overlap, len(y_raw))
    for n in range(lo, hi + 1):
        raw = y_raw[n - 1]
        cn = re.sub(r"\s+", "", strip_cn(raw))
        jp_line = jp[n - 1] if paired else None
        if not cn and not jp_line:
            continue  # 双空行无信息（CN-only 段空行同此）
        marks = []
        if n < part.start or n > part.end:
            marks.append("ctx")
        if "<h1" in raw or "<h2" in raw:
            marks.append("H")
        prefix = " ".join(marks) + " " if marks else ""
        out.append(f"{prefix}L{n} CN: {cn}")
        if paired:
            out.append(f"{prefix}L{n} JP: {jp_line}")
    return out


def write_pair_files(
    chunks: list[Chunk], code: str, aligned_dir: Path, overlap: int
) -> list[Path]:
    """逐块写对照文件；先清掉旧块（上轮块数可能更多），返回与 chunks 平行的路径表。"""
    out_dir = CHUNK_DIR / code
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("chunk-*.txt"):
        stale.unlink()
    paths = []
    for i, c in enumerate(chunks, 1):
        p = out_dir / f"chunk-{i:02d}.txt"
        lines = [
            f"# chunk-{i:02d} 中日逐行对照材料（派生自 Y 产物与 ../index-jp/.cache/bw_aligned/{code}）",
            "# 行号=Y 源文件物理行号，两侧一一对应；CN=Y 剥标签文本（ruby 注音连写在基文后，如 空间移动Teleport）；JP=日文原文同行剥标签文本",
            "# ctx=重叠上下文（只读不报）；H=标题行（h1/h2）；双空行已跳过；CN/JP 一空一非空=漏译或行号失配信号，照常列出",
            "# findings 的 file 填各段 == 横幅里的源文件名，line 填 L 行号",
        ]
        for part in c.parts:
            lines.append("")
            lines.extend(pair_lines(part, aligned_dir, overlap))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("vol", help="卷目录名或卷码（如 S1_02）")
    ap.add_argument(
        "--target-chars",
        type=int,
        default=None,
        help="每块目标中文字符数（默认：对照卷 12000，未对齐卷 15000）",
    )
    ap.add_argument("--overlap", type=int, default=10, help="上下文重叠行数")
    ap.add_argument(
        "--max-chunks",
        type=int,
        default=10,
        help="并行块数软上限（默认：%(default)d；目标超过 40000 字符后停止重切）",
    )
    args = ap.parse_args()

    vol, text_dir = resolve_text_dir(args.vol)
    files = content_files(text_dir)
    code = jp_align.vol_code(vol)
    aligned_dir = (
        jp_align.aligned_vols(ROOT.parent / "index-jp").get(code) if code else None
    )
    target = args.target_chars or (12000 if aligned_dir else 15000)
    while True:
        chunks = plan(files, target)
        if len(chunks) <= args.max_chunks or target > 40000:
            break
        target += 2000

    pair_paths = None
    if aligned_dir:
        assert code is not None  # aligned_dir 由 code 查得，必非 None
        pair_paths = write_pair_files(chunks, code, aligned_dir, args.overlap)

    total_chars = sum(c.chars for c in chunks)
    print(
        f"{vol}: {len(files)} 个正文文件 -> {len(chunks)} 块"
        f"（目标 {target} 字符/块，共约 {total_chars // 1000}k 字符）"
    )
    if pair_paths:
        print(f"对照文件目录（派生产物，已清理旧块重写）: {pair_paths[0].parent}")
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
        if pair_paths:
            main = "、".join(f"{p.path.name} L{p.start}-L{p.end}" for p in c.parts)
            print(
                f"     goal: 校对通读 {pair_paths[i - 1].as_posix()}"
                f"（中日逐行对照文件，约 {c.chars // 1000} 千中文字符；校对主区间：{main}；"
                "findings 的 file/line 按对照文件内各段 == 横幅的源文件名与 L 行号填报）"
            )
        else:
            goal = "；".join(
                f"{p.path} 第 {p.start}-{p.end} 行"
                + ("" if p.whole else "（前后各约 10 行为重叠上下文，发现只报主区间）")
                for p in c.parts
            )
            print(f"     goal: 校对通读 {goal}（约 {c.chars // 1000} 千字符）")

    # 全部块共用的分派参数：context 短指令（子 agent 自载模板）+ output_schema。
    # schema 单一来源是模板【output_schema】节，此处提取后原样转印。
    template = (
        ROOT
        / ".agents"
        / "skills"
        / "agent-proofread"
        / "templates"
        / "chapter-agent-prompt.md"
    ).read_text(encoding="utf-8")
    m = re.search(r"【output_schema】[^\n]*\n(\{.*?\})\s*$", template, flags=re.DOTALL)
    if not m:
        sys.exit("模板【output_schema】节提取失败，请检查模板尾部格式")
    schema = re.sub(r"\s+", "", m.group(1))
    vol_prefix = vol.split("】")[0] + "】"
    how = (
        "goal 给出中日逐行对照文件路径与校对主区间，按对照文件逐行对照通读；"
        "findings 的 file/line 按对照文件内各段 == 横幅的源文件名与 L 行号填报。"
        if pair_paths
        else "goal 给出的行区间即你的校对主区间，逐行通读（模板【无行号对齐卷模式】节）；"
        "疑点回原文核对用模板规定的 proofreading_locate.py 批量清单法。"
    )
    print("\ncontext（全部块共用，逐字复制进每个 task）:")
    print(
        f"按本仓库 .agents/skills/agent-proofread/templates/chapter-agent-prompt.md 模板执行"
        f'（先 skill_view(name="agent-proofread", file_path="templates/chapter-agent-prompt.md") 加载）：'
        f"模板中 {{{{VOL_PREFIX}}}} 替换为 {vol_prefix}。"
        + how
        + "final answer 严格输出模板【产出】节规定的 JSON，不要多余文字。不修改任何文件。"
    )
    print("\noutput_schema（全部块共用，逐字复制进每个 task）:")
    print(schema)
    return 0


if __name__ == "__main__":
    sys.exit(main())
