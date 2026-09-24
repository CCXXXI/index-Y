"""agent 通读校对的发现复核：对子 agent 上报的 findings JSON 做机械验证。

输入 findings JSON（UTF-8 数组，每条含 file/line/quote，可选 jp_evidence 或 jp），
逐条验证并输出带结论标记的 JSON：

- 中文引文逐字验证：目标卷 Y 侧对应文件 line ±3 行 raw 匹配；不匹配则剥标签
  （保留 <rt> 内容）±5 行再匹配——引文跨 ruby 标签或跨行属常见，raw 不匹配≠编造；
- 日文依据验证：原文语料（默认 ../index-jp，--jp-root 覆盖）剥 <rt>、去标签、
  归并空白后做子串匹配；精确匹配失败再用「读音容忍」正则兜底——原文仓库部分
  文件的读音写在 <rt> 之外（如 巫女みこさん、瞬間錬金リメン＝マグナ），兜底正则
  允许在证据字符间插入假名/长音/＝，仅靠兜底救回的条目打 jp_flex 标记；
- X/Y 比对：同一行 X 侧与 Y 侧剥标签文本是否一致（区分上游问题与规则引入）。

只输出事实标记（quote_ok/jp_ok/x_same/found_at），不下最终判定；判定归 agent。

用法：uv run python scripts/agent_proofread/verify_findings.py <卷> findings.json [-o out.json]
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

from tqdm import tqdm

ROOT = Path(__file__).parent.parent.parent
TEXT_EXT = (".xhtml", ".html", ".htm")


def strip_cn(line: str) -> str:
    """中文侧剥标签：保留 <rt> 注音内容（注音是译文的拉丁写法，参与匹配）。"""
    t = re.sub(r"<rt>(.*?)</rt>", r"\1", line, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(re.sub(r"\s+", "", t))


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
    matches = [
        d.name for d in x_dir.iterdir() if d.is_dir() and d.name.startswith(vol_arg)
    ]
    if len(matches) != 1:
        sys.exit(
            f"卷参数 {vol_arg!r} 在 index-X/EPUB/ 下匹配到 {len(matches)} 个目录: {matches}"
        )
    return matches[0]


def jp_corpus(jp_root: Path, vol: str) -> str:
    """同卷原文目录名前缀与 index-X/EPUB/ 卷目录一致（如 [S1_01]）。"""
    prefix = vol.split("]")[0] + "]"
    dirs = [d for d in jp_root.iterdir() if d.is_dir() and d.name.startswith(prefix)]
    if not dirs:
        print(
            f"警告：{jp_root} 下未找到 {prefix} 原文目录，jp 验证全部跳过",
            file=sys.stderr,
        )
        return ""
    parts = []
    for d in dirs:
        for p in tqdm(sorted(d.rglob("*")), desc="jp 扫描"):
            if p.suffix.lower() in TEXT_EXT and p.is_file():
                parts.append(norm_jp(p.read_text(encoding="utf-8", errors="ignore")))
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("vol", help="卷目录名或 [Sx_yy] 前缀")
    ap.add_argument("findings", type=Path, help="findings JSON（数组）")
    ap.add_argument(
        "-o", "--out", type=Path, help="输出 JSON（默认 <findings>.verified.json）"
    )
    ap.add_argument("--jp-root", type=Path, default=ROOT.parent / "index-jp")
    args = ap.parse_args()

    vol = resolve_vol(args.vol, ROOT / "index-X" / "EPUB")
    findings: list[dict] = json.loads(args.findings.read_text(encoding="utf-8-sig"))
    y_dir = ROOT / "EPUB" / vol
    x_dir = ROOT / "index-X" / "EPUB" / vol
    jp = (
        jp_corpus(args.jp_root, vol)
        if any(f.get("jp_evidence") or f.get("jp") for f in findings)
        else ""
    )

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
        if "jp" not in f and f.get("jp_evidence"):
            f["jp"] = f["jp_evidence"]
        y_lines = lines_of(y_dir, f["file"])
        ln = int(f["line"])
        found_at = None
        for j in range(max(0, ln - 4), min(len(y_lines), ln + 3)):
            if f["quote"] in y_lines[j]:
                found_at = j + 1
                break
        f["quote_ok"] = found_at is not None
        if not f["quote_ok"]:  # 剥标签容差（跨 ruby/跨行引文）
            q = re.sub(r"\s+", "", f["quote"])
            for j in range(max(0, ln - 6), min(len(y_lines), ln + 5)):
                if q in strip_cn(y_lines[j]):
                    found_at, f["quote_ok"] = j + 1, True
                    break
        f["found_at"] = found_at
        if not f["quote_ok"]:
            bad_quote += 1
        if f.get("jp"):
            needle = norm_jp(f["jp"])
            f["jp_ok"] = (needle in jp) if jp else None
            if f["jp_ok"] is False and jp_flex_ok(needle, jp):
                f["jp_ok"], f["jp_flex"] = True, True
            if f["jp_ok"] is False:
                bad_jp += 1
        else:
            f["jp_ok"] = None
        if found_at:
            x_text = strip_cn(lines_of(x_dir, f["file"])[found_at - 1])
            f["x_same"] = x_text == strip_cn(y_lines[found_at - 1])
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
