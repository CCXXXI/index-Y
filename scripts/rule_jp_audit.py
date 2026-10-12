"""规则日文依据核查：全部生效规则 × 当前 index-jp 原文。

背景：index-jp 日文资源历经多次更新（如 82b9c4f 旧约 13 卷 BW 替换），部分规则的
注释引文或判定前提基于旧版原文。本脚本生成「规则 × 命中点采样 × 当前原文」核查单元，
供批量 LLM 判定；判定结果经 verify 子命令机械复核（引文逐字验证）后人工分诊。

用法：
    uv run python scripts/rule_jp_audit.py units          # 生成单元 + 批次文件
    uv run python scripts/rule_jp_audit.py verify 判定.json  # 机械复核 LLM 判定

单元口径：
- 生效 ≈ 规则 old（正则）在适用卷 X 文本（.xhtml/.opf/.ncx）有命中（近似口径，
  比 active_rules.py 的「移除后终态改变」略宽；失活规则即无命中规则自动排除）。
- 每条规则采样 ≤3 个命中行：优先有行号对齐（bw_aligned pass）的卷、优先正文
  xhtml、卷间尽量分散；命中总数与命中卷清单一并给出。
- jp 定位：对齐卷经 jp_align.jp_at 行号直读（±1 行上下文）；包装页/元数据文件
  无对应单元（metadata）；非对齐卷标 search_needed；无原文豁免卷标 no_original。
- 引文核查：注释中「」引文（含假名）与裸日文片段，对当前语料逐字核验；语料归
  一化剥 rt/去标签/归并空白（proofreading_locate 姿势），另建 rt 内联变体覆盖
  注音引文；引文先原样试，再试「（x）→x」与「去括号段」变体（旧资源括号读音 vs
  BW ruby 的表示差异）。
"""

import argparse
import html
import json
import sys
from pathlib import Path

import jp_align
import regex as re
from sync.x2y import load_rule_records

ROOT = Path(__file__).parent.parent
X_DIR = ROOT / "index-X" / "EPUB"
JP_ROOT = ROOT.parent / "index-jp"
TEXT_EXT = (".xhtml", ".opf", ".ncx")
SAMPLE_PER_RULE = 3
BATCH_SIZE = 60
X_LINE_CAP = 400  # 命中行截断（围绕命中点开窗）

KANA = re.compile(r"[ぁ-んァ-ヶ]")
QUOTE = re.compile(r"「([^」]+)」")
SPLICE = re.compile(r"…|\.\.\.|・・・")
SEG_SPLIT = re.compile(r"[\s，。；：、（）()「」『』【】《》〈〉!?！？…—/=~・|]+")
JP_RUN = re.compile(r"[一-龥々〆ヵヶぁ-んァ-ヶーゝゞヽヾ]+")
GLOSS = re.compile(r"（[^）]*）")
META_CHAR = re.compile(r"[.\[\](){}^$*+?|\\]")


# ---------- 语料 ----------


def strip_jp(raw: str, keep_rt: bool = False) -> str:
    if keep_rt:
        t = re.sub(r"<rt>(.*?)</rt>", r"\1", raw, flags=re.DOTALL)
    else:
        t = re.sub(r"<rt>.*?</rt>", "", raw, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", "", html.unescape(t))


def build_jp_blobs() -> tuple[str, str]:
    """全语料两种变体拼接（剥 rt / rt 内联），供引文逐字核验。"""
    no_rt, rt = [], []
    for d in sorted(JP_ROOT.iterdir()):
        if not d.is_dir() or not jp_align.vol_code(d.name):
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix in TEXT_EXT:
                raw = f.read_text(encoding="utf-8", errors="replace")
                no_rt.append(strip_jp(raw))
                rt.append(strip_jp(raw, keep_rt=True))
    return "".join(no_rt), "".join(rt)


# ---------- 规则命中扫描 ----------


def literal_core(pat: str) -> str:
    """最长无元字符连续段，做文件级预筛（近似，不处理转义细节）。"""
    best, cur = "", ""
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and i + 1 < len(pat):
            cur = ""
            i += 2
            continue
        if META_CHAR.match(c):
            cur = ""
        else:
            cur += c
            if len(cur) > len(best):
                best = cur
        i += 1
    return best


def scan_hits(rules: list[dict]) -> None:
    """就地填 rule['hit_list'] = [{vol, rel, line, text}]。"""
    vols = sorted(p.name for p in X_DIR.iterdir() if p.is_dir())
    for r in rules:
        r["rx"] = re.compile(r["old"])
        r["core"] = literal_core(r["old"])
        r["hit_list"] = []
    for vol in vols:
        vol_rules = [r for r in rules if r["section"] in (vol, "*")]
        files = sorted(p for p in (X_DIR / vol).rglob("*") if p.suffix in TEXT_EXT)
        for p in files:
            text = p.read_text(encoding="utf-8")
            cand = [
                r
                for r in vol_rules
                if (not r["core"] or r["core"] in text) and r["rx"].search(text)
            ]
            if not cand:
                continue
            rel = p.relative_to(X_DIR / vol).as_posix()
            for lineno, ln in enumerate(text.splitlines(), 1):
                for r in cand:
                    m = r["rx"].search(ln)
                    if m:
                        r["hit_list"].append(
                            {
                                "vol": vol,
                                "rel": rel,
                                "line": lineno,
                                "pos": m.start(),
                                "text": ln,
                            }
                        )


# ---------- 命中采样与 jp 预取 ----------


def sample_hits(hits: list[dict], aligned: dict[str, Path]) -> list[dict]:
    """≤3 个命中：优先对齐卷、优先正文 xhtml，卷间分散，确定性。"""

    def rank(h):
        code = jp_align.vol_code(h["vol"])
        is_aligned = 0 if code in aligned else 1
        is_text = 0 if h["rel"].endswith(".xhtml") and "Text/" in h["rel"] else 1
        return (is_aligned, is_text, h["vol"], h["rel"], h["line"])

    ordered = sorted(hits, key=rank)
    picked, seen_vols = [], set()
    for h in ordered:  # 第一轮：每卷最多一个
        if len(picked) >= SAMPLE_PER_RULE:
            break
        if h["vol"] not in seen_vols:
            picked.append(h)
            seen_vols.add(h["vol"])
    for h in ordered:  # 第二轮：补足
        if len(picked) >= SAMPLE_PER_RULE:
            break
        if h not in picked:
            picked.append(h)
    return picked


def jp_for_hit(hit: dict, aligned: dict[str, Path]) -> dict:
    """命中点 → 当前日文原文（对齐卷行号直读）。"""
    code = jp_align.vol_code(hit["vol"])
    if code in aligned:
        unit = jp_align.unit_code(Path(hit["rel"]).name)
        if unit:
            got = jp_align.jp_at(aligned[code], unit, hit["line"], ctx=1)
            if got:
                jp_lines = []
                for c in got["context"]:
                    m = re.match(r"L(\d+): (.*)", c, flags=re.DOTALL)
                    if m:
                        jp_lines.append({"L": int(m.group(1)), "text": m.group(2)})
                return {"status": "aligned", "jp": jp_lines}
        return {"status": "metadata"}  # 包装页/元数据无对应 jp 单元
    jp_dirs = [
        d for d in JP_ROOT.iterdir() if d.is_dir() and jp_align.vol_code(d.name) == code
    ]
    return {"status": "search_needed"} if jp_dirs else {"status": "no_original"}


def cap_x_text(hit: dict) -> str:
    t = hit["text"]
    if len(t) <= X_LINE_CAP:
        return t
    s = max(0, hit["pos"] - X_LINE_CAP // 2)
    return f"…{t[s : s + X_LINE_CAP]}…"


# ---------- 注释引文核查 ----------


def extract_citations(comment: str) -> list[tuple[str, str]]:
    """「」引文（含假名，…拼接拆分）+ 裸日文片段（含假名的日文连续段）。

    返回 (frag, kind)：kind=quoted 为逐字引文声明（严格核验）；
    kind=bare 为注释中提及的日文词（允许混排中文解说，按最长命中子串给覆盖度）。
    """
    frags: list[tuple[str, str]] = []
    for q in QUOTE.findall(comment):
        if KANA.search(q):
            frags += [(f, "quoted") for f in SPLICE.split(q) if f and KANA.search(f)]
    rest = QUOTE.sub(" ", comment)
    for seg in SEG_SPLIT.split(rest):
        if KANA.search(seg) and JP_RUN.fullmatch(seg):
            frags.append((seg, "bare"))
    seen, out = set(), []
    for f, k in frags:
        f = re.sub(r"\s+", "", f).strip("。、")
        if len(f) >= 2 and (f, k) not in seen:
            seen.add((f, k))
            out.append((f, k))
    return out


def _longest_hit(frag: str, blob: str, blob_rt: str) -> tuple[str, int]:
    """frag 在语料中的最长命中子串（含假名，len>=2），返回 (子串, 覆盖度分母 len(frag))。"""
    best = ""
    n = len(frag)
    for length in range(n, 1, -1):
        for s in range(n - length + 1):
            sub = frag[s : s + length]
            if not KANA.search(sub):
                continue
            if sub in blob or sub in blob_rt:
                return sub, n
    return best, n


def check_citation(frag: str, kind: str, blob_no_rt: str, blob_rt: str) -> dict:
    """quoted 严格核验（原样 / （x）→x / 去括号段）；bare 报最长命中覆盖度。"""
    if kind == "quoted":
        variants = [
            ("verbatim", frag),
            ("gloss_inline", GLOSS.sub(lambda m: m.group(0)[1:-1], frag)),
            ("gloss_strip", GLOSS.sub("", frag)),
        ]
        for vkind, v in variants:
            if len(v) >= 2 and KANA.search(v):
                if v in blob_no_rt:
                    return {"frag": frag, "kind": kind, "found": True, "variant": vkind}
                if v in blob_rt:
                    return {
                        "frag": frag,
                        "kind": kind,
                        "found": True,
                        "variant": vkind + "+rt",
                    }
        return {"frag": frag, "kind": kind, "found": False}
    sub, n = _longest_hit(frag, blob_no_rt, blob_rt)
    cov = len(sub) / n if n else 0
    return {
        "frag": frag,
        "kind": kind,
        "found": cov >= 0.95,
        "coverage": round(cov, 2),
        "matched": sub,
    }


# ---------- units 子命令 ----------


def cmd_units(out_dir: Path) -> None:
    rules = load_rule_records()
    print(f"rules={len(rules)}", flush=True)
    scan_hits(rules)
    active = [r for r in rules if r["hit_list"]]
    print(
        f"active={len(active)} hit_lines={sum(len(r['hit_list']) for r in active)}",
        flush=True,
    )
    aligned = jp_align.aligned_vols(JP_ROOT)
    print(f"aligned_vols={len(aligned)}", flush=True)
    blob_no_rt, blob_rt = build_jp_blobs()
    print(f"jp_blob={len(blob_no_rt)} chars", flush=True)

    units: list[dict] = []
    for r in active:
        code = jp_align.vol_code(r["section"]) if r["section"] != "*" else "_common"
        uid = f"{code}:{r['lineno']}"
        sampled = sample_hits(r["hit_list"], aligned)
        vols = {}
        for h in r["hit_list"]:
            vols[h["vol"]] = vols.get(h["vol"], 0) + 1
        citations = [
            check_citation(f, k, blob_no_rt, blob_rt)
            for f, k in extract_citations(r["note"])
        ]
        units.append(
            {
                "id": uid,
                "old": r["old"],
                "new": r["new"],
                "note": r["note"],
                "hits_total": len(r["hit_list"]),
                "hits_by_vol": vols,
                "samples": [
                    {
                        "vol": jp_align.vol_code(h["vol"]),
                        "file": h["rel"],
                        "line": h["line"],
                        "x_text": cap_x_text(h),
                        **jp_for_hit(h, aligned),
                    }
                    for h in sampled
                ],
                "citations": citations,
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "units.json").write_text(
        json.dumps(units, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    batches = [units[i : i + BATCH_SIZE] for i in range(0, len(units), BATCH_SIZE)]
    for i, b in enumerate(batches, 1):
        (out_dir / f"batch_{i:02d}.json").write_text(
            json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    n_cit = sum(1 for u in units if u["citations"])
    n_cit_miss = sum(
        1
        for u in units
        if u["citations"] and any(not c["found"] for c in u["citations"])
    )
    statuses = {}
    for u in units:
        for s in u["samples"]:
            statuses[s["status"]] = statuses.get(s["status"], 0) + 1
    print(f"units={len(units)} batches={len(batches)}")
    print(f"with_citations={n_cit} citation_suspect={n_cit_miss}")
    print(f"sample jp status: {statuses}")
    print(f"out: {out_dir}")


# ---------- verify 子命令 ----------


def cmd_verify(units_path: Path, verdicts_path: Path) -> None:
    units = {u["id"]: u for u in json.loads(units_path.read_text(encoding="utf-8"))}
    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    if isinstance(verdicts, dict):
        verdicts = verdicts.get("verdicts", verdicts.get("results", []))

    errors, seen = [], set()
    aligned = jp_align.aligned_vols(JP_ROOT)
    corpus_cache: dict[str, str] = {}

    def vol_corpus(code: str) -> str:
        if code not in corpus_cache:
            corpus_cache[code] = jp_align.aligned_corpus(JP_ROOT, code)
        return corpus_cache[code]

    for v in verdicts:
        vid, verdict = v.get("id"), v.get("verdict")
        if vid not in units:
            errors.append(f"未知 id: {vid}")
            continue
        if vid in seen:
            errors.append(f"重复判定: {vid}")
        seen.add(vid)
        if verdict not in ("ok", "wrong", "stale_evidence", "uncertain"):
            errors.append(f"{vid}: 非法 verdict {verdict!r}")
        ev = v.get("jp_evidence")
        if verdict in ("wrong", "stale_evidence") and not ev:
            errors.append(f"{vid}: verdict={verdict} 缺 jp_evidence")
        if ev:
            ev_n = re.sub(r"\s+", "", ev)
            u = units[vid]
            in_sample = any(
                ev_n in re.sub(r"\s+", "", j["text"])
                for s in u["samples"]
                for j in s.get("jp", [])
            )
            if not in_sample:
                # 兜底：命中卷的对齐语料
                codes = {
                    c for c in (s["vol"] for s in u["samples"]) if c and c in aligned
                }
                if not any(ev_n in vol_corpus(c) for c in codes):
                    errors.append(f"{vid}: jp_evidence 非所给原文逐字（疑似编造）")
    missing = set(units) - seen
    print(f"units={len(units)} verdicts={len(verdicts)} covered={len(seen)}")
    if missing:
        print(f"未判定 {len(missing)}: {sorted(missing)[:10]}...")
    counts = {}
    for v in verdicts:
        counts[v.get("verdict")] = counts.get(v.get("verdict"), 0) + 1
    print(f"verdict 分布: {counts}")
    if errors:
        print(f"\n复核不通过 {len(errors)} 项：")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    print("复核全部通过")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    pu = sub.add_parser("units", help="生成核查单元与批次文件")
    pu.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".cache" / "rule_jp_audit",
        help="单元/批次输出目录（运行产物，不入库）",
    )
    pv = sub.add_parser("verify", help="机械复核 LLM 判定")
    pv.add_argument("verdicts", type=Path, help="汇总的判定 JSON")
    pv.add_argument(
        "--units",
        type=Path,
        default=Path.home() / ".cache" / "rule_jp_audit" / "units.json",
    )
    args = p.parse_args()
    if args.cmd == "units":
        cmd_units(args.out)
    else:
        cmd_verify(args.units, args.verdicts)


if __name__ == "__main__":
    main()
