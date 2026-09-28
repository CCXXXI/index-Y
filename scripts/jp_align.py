"""BW 行号对齐共享库：index-jp .cache/bw_aligned pass 卷的注册表与行号直读。

index-jp `scripts/bw_align.py` 复用上游对齐管线，把 BW 分页格式卷重建为与中文侧
**行号一一对应**的日文章节单元（`.cache/bw_aligned/<卷码>/`，manifest.json 中
status=pass 的卷经 check_alignment 门禁：行数/h2/图片行/br 位置全等）。

本库统一各校对流程对该产物的消费（prepare_review 行号直读、proofreading_locate
直读标注、verify_findings 语料并入等）：

- `aligned_vols(jp_root)`：pass 卷注册表 卷码 → 对齐单元目录
- `unit_code(name)`：译文文件名 → 内容序单元码（S3_04-09）
- `strip_line(line)`：单行 xhtml → 剥 <rt>/标签、归并空白的纯文本
- `jp_at(jp_root, vol, unit, line, ctx)`：行号直读 → {line, jp, context}
- `aligned_corpus(jp_root, vol)`：全卷对齐剥标签文本（供逐字核验语料并入）
"""

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
UNIT_RE = re.compile(r"(S\d+_\d+(?:_\d+)?|S6_\d{2}\.\d{2}\.\d{2})-(\d+)", re.IGNORECASE)


def unit_code(name: str) -> str | None:
    """译文文件名 → 内容序单元码（S3_04-09_Chapter4.xhtml → S3_04-09）。"""
    m = UNIT_RE.search(Path(name).name)
    return f"{m.group(1).upper()}-{m.group(2)}" if m else None


def aligned_vols(jp_root: Path) -> dict[str, Path]:
    """bw_aligned pass 卷：卷码 → 对齐单元目录（行号与译文一一对应）。"""
    mf = jp_root / ".cache" / "bw_aligned" / "manifest.json"
    if not mf.exists():
        return {}
    m = json.loads(mf.read_text(encoding="utf-8"))
    return {
        v: jp_root / ".cache" / "bw_aligned" / v
        for v, r in m.items()
        if r.get("status") == "pass"
    }


def strip_line(line: str) -> str:
    """单行 xhtml → 剥 <rt> 注音、去标签、归并空白的纯文本。"""
    t = re.sub(r"<rt>.*?</rt>", "", line, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(re.sub(r"\s+", "", t))


def unit_stripped_lines(aligned_dir, unit: str) -> list[str] | None:
    """对齐单元文件的剥标签行（保留空行占位，行号与译文物理行一致）。"""
    p = Path(aligned_dir) / f"{unit}.xhtml"
    if not p.exists():
        return None
    return [strip_line(ln) for ln in p.read_text(encoding="utf-8").splitlines()]


def jp_at(aligned_dir, unit: str, line: int, ctx: int = 2) -> dict | None:
    """行号直读：译文物理行号（1 起）→ {line, jp, context（L行号: 文本）}。"""
    lines = unit_stripped_lines(aligned_dir, unit)
    if lines is None or not (1 <= line <= len(lines)):
        return None
    i = line - 1
    context = []
    for k in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
        if lines[k]:
            context.append(f"L{k + 1}: {lines[k]}")
    return {"line": line, "jp": lines[i], "context": context}


def locate(lines: list[str], anchors: list[str]) -> int | None:
    """在剥标签行序列中按锚点唯一定位：逐个尝试（调用方按长度降序给），
    返回唯一命中行号（1 起）；无唯一命中返回 None。"""
    for a in anchors:
        found = [i for i, ln in enumerate(lines) if a and a in ln]
        if len(found) == 1:
            return found[0] + 1
    return None


def aligned_corpus(jp_root: Path, vol: str) -> str:
    """全卷对齐剥标签文本拼接（供 jp 引文逐字核验语料并入）；非 pass 卷返回 ""。"""
    d = aligned_vols(jp_root).get(vol)
    if d is None:
        return ""
    parts = []
    for p in sorted(d.glob("*.xhtml")):
        parts.append(strip_line(p.read_text(encoding="utf-8")))
    return "\n".join(parts)
