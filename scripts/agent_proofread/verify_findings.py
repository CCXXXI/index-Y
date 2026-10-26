"""agent 通读校对的发现复核：对子 agent 上报的 findings JSON 做机械验证。

输入 findings JSON（UTF-8 数组，每条含 file/line/quote，可选 jp_evidence 或 jp），
逐条验证并输出带结论标记的 JSON：

- 中文引文逐字验证：目标卷 Y 侧对应文件 line ±3 行 raw 匹配；不匹配则剥标签
  （保留 <rt> 内容）±5 行再匹配——引文跨 ruby 标签或跨行属常见，raw 不匹配≠编造；
- 日文依据验证：原文语料（默认 ../index-jp，--jp-root 覆盖）剥 <rt>、去标签、
  归并空白后做子串匹配；精确匹配失败再用「读音容忍」正则兜底——原文仓库部分
  文件的读音写在 <rt> 之外（如 巫女みこさん、瞬間錬金リメン＝マグナ），兜底正则
  允许在证据字符间插入假名/长音/＝，仅靠兜底救回的条目打 jp_flex 标记；
- X/Y 比对：引文覆盖的全部物理行在 X 侧与 Y 侧剥标签文本是否一致（区分上游问题与规则引入）。

只输出事实标记（quote_ok/jp_ok/x_same/found_at/found_end），不下最终判定；判定归 agent。

用法：uv run python scripts/agent_proofread/verify_findings.py <卷> findings.json [-o out.json]
"""

import argparse
import html
import json
import re
import sys
from bisect import bisect_right
from pathlib import Path

from tqdm import tqdm

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import jp_align

TEXT_EXT = (".xhtml", ".html", ".htm")


def strip_cn(line: str) -> str:
    """中文侧剥标签：保留 <rt> 注音内容（注音是译文的拉丁写法，参与匹配）。"""
    t = re.sub(r"<rt>(.*?)</rt>", r"\1", line, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(re.sub(r"\s+", "", t))


def find_quote_range(
    lines: list[str], needle: str, *, stripped: bool
) -> tuple[int, int] | None:
    """在窗口内匹配单行/跨行引文，返回窗口内 1 起始、含端点的行号范围。"""
    if not needle:
        return None
    parts = [strip_cn(line) for line in lines] if stripped else lines
    separator = "" if stripped else "\n"
    starts = []
    offset = 0
    for part in parts:
        starts.append(offset)
        offset += len(part) + len(separator)
    found = separator.join(parts).find(needle)
    if found < 0:
        return None
    return bisect_right(starts, found), bisect_right(starts, found + len(needle) - 1)


def norm_jp(raw: str) -> str:
    """原文侧预处理：剥 <rt> 注音、去标签、归并空白（index-jp 对照姿势）。"""
    t = re.sub(r"<rt>.*?</rt>", "", raw, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(re.sub(r"\s+", "", t))


# 兜底正则允许插入的字符：假名、长音、＝（原文 <rt> 之外读音的书写惯例，
# 如 巫女みこさん、量産聖槍ロンギヌス＝レプリカ、瞬間錬金リメン＝マグナ）。
_FLEX_INS = r"[ぁ-んァ-ヶー＝]*"


def jp_flex_ok(evidence: str, corpus: str) -> bool:
    """证据字符间允许插入读音的容忍匹配（精确子串匹配失败后的兜底）。"""
    pat = _FLEX_INS + _FLEX_INS.join(re.escape(c) for c in evidence) + _FLEX_INS
    return re.search(pat, corpus) is not None


def resolve_vol(vol_arg: str, x_dir: Path) -> str:
    return jp_align.resolve_vol(vol_arg, x_dir)


def jp_corpus(jp_root: Path, vol: str) -> str:
    """同卷原文目录与中文卷同卷码。"""
    code = jp_align.vol_code(vol)
    dirs = [
        d for d in jp_root.iterdir() if d.is_dir() and jp_align.vol_code(d.name) == code
    ]
    if not dirs:
        print(
            f"警告：{jp_root} 下未找到 {code} 原文目录，jp 验证全部跳过",
            file=sys.stderr,
        )
        return ""
    parts = []
    for d in dirs:
        for p in tqdm(sorted(d.rglob("*")), desc="jp 扫描"):
            if p.suffix.lower() in TEXT_EXT and p.is_file():
                parts.append(norm_jp(p.read_text(encoding="utf-8", errors="ignore")))
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("vol", help="卷目录名或卷码（如 S1_02）")
    ap.add_argument("findings", type=Path, help="findings JSON（数组）")
    ap.add_argument(
        "-o", "--out", type=Path, help="输出 JSON（默认 <findings>.verified.json）"
    )
    ap.add_argument("--jp-root", type=Path, default=ROOT.parent / "index-jp")
    args = ap.parse_args()

    vol = resolve_vol(args.vol, ROOT / "index-X" / "EPUB")
    code = jp_align.vol_code(vol)
    assert code is not None  # vol 来自 resolve_vol，必含卷码
    findings: list[dict] = json.loads(args.findings.read_text(encoding="utf-8-sig"))
    y_dir = ROOT / "EPUB" / vol
    x_dir = ROOT / "index-X" / "EPUB" / vol
    jp = (
        jp_corpus(args.jp_root, vol)
        if any(f.get("jp_evidence") or f.get("jp") for f in findings)
        else ""
    )
    if jp:
        # pass 卷并入行号对齐语料（子代理行号直读引文同样逐字可验）
        jp += "\n" + jp_align.aligned_corpus(args.jp_root, code)
    aligned_dir = jp_align.aligned_vols(args.jp_root).get(code)

    line_cache: dict[str, list[str]] = {}

    def lines_of(side: Path, name: str) -> list[str]:
        key = f"{side}|{name}"
        if key not in line_cache:
            hits = list(side.rglob(name))
            if len(hits) != 1:
                sys.exit(f"{side} 下 {name} 命中 {len(hits)} 个")
            line_cache[key] = hits[0].read_text(encoding="utf-8").split("\n")
        return line_cache[key]

    bad_quote = bad_jp = 0
    for f in findings:
        # 归一化字段名
        if "jp" not in f and f.get("jp_evidence") is not None:
            f["jp"] = f["jp_evidence"]
        y_lines = lines_of(y_dir, f["file"])
        ln = int(f["line"])
        found_at = found_end = None
        q = re.sub(r"\s+", "", f["quote"])
        raw_lo = max(0, ln - 4)
        raw_window = y_lines[raw_lo : min(len(y_lines), ln + 3)]
        raw_hit = find_quote_range(raw_window, f["quote"] if q else "", stripped=False)
        if raw_hit is not None:
            found_at = raw_lo + raw_hit[0]
            found_end = raw_lo + raw_hit[1]
        f["quote_ok"] = found_at is not None
        if not f["quote_ok"]:  # 剥标签容差（跨 ruby/跨行引文）
            stripped_lo = max(0, ln - 6)
            stripped_window = y_lines[stripped_lo : min(len(y_lines), ln + 5)]
            stripped_hit = find_quote_range(stripped_window, q, stripped=True)
            if stripped_hit is not None:
                found_at = stripped_lo + stripped_hit[0]
                found_end = stripped_lo + stripped_hit[1]
                f["quote_ok"] = True
        f["found_at"] = found_at
        f["found_end"] = found_end
        if not f["quote_ok"]:
            bad_quote += 1
        if f.get("jp") is not None:
            needle = norm_jp(f["jp"])
            f["jp_ok"] = (needle in jp) if jp and needle else None
            if not needle:
                f["jp_ok"] = False
            if f["jp_ok"] is False and needle and jp_flex_ok(needle, jp):
                f["jp_ok"], f["jp_flex"] = True, True
            if f["jp_ok"] is False:
                bad_jp += 1
        else:
            f["jp_ok"] = None
        if found_at:
            assert found_end is not None
            x_lines = lines_of(x_dir, f["file"])[found_at - 1 : found_end]
            y_quote_lines = y_lines[found_at - 1 : found_end]
            f["x_same"] = [strip_cn(line) for line in x_lines] == [
                strip_cn(line) for line in y_quote_lines
            ]
            # 行号直读（pass 卷）：Y 行号 ≡ JP 行号由 check_y_jp_alignment 门禁
            # （行数一致）与上游 check_alignment（X↔JP 位置配对）共同保证，
            # 直接用 Y 物理行号；规则效果行同样成立（锚定法反而会巧合命中他行）
            if aligned_dir is not None:
                unit = jp_align.unit_code(f["file"])
                if unit:
                    got = jp_align.jp_at(aligned_dir, unit, found_at)
                    if got:
                        f["jp_direct"] = got
        else:
            f["x_same"] = None

    out = args.out or args.findings.with_suffix(".verified.json")
    out.write_text(json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"共 {len(findings)} 条：引文不符 {bad_quote}，日文不符 {bad_jp}，"
        f"X≠Y {sum(1 for f in findings if f['x_same'] is False)} 条 -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
