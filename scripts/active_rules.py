"""报告指定范围内当前生效的 x2y 规则及其首次生效点。

失活规则保留在 rules/ 中不删除（见 report_inactive_rules），因此 rules/ 不再等同于
「当前生效的规则清单」；向上游反馈某卷/某章的修订时用本脚本实测。

「生效」定义与 report_inactive_rules.py 一致：把该规则从管道
（分卷段先于通用段，段内自上而下）中移除后，范围内某文本文件的最终
输出发生改变；仅命中但被后续规则再收敛的不算生效。扫描对象为工作区 X
中的 .xhtml/.opf/.ncx（与 x2y.py 的 TEXT_EXT 一致）。
"""

import argparse
import io
import sys
from pathlib import Path

import colorama
import regex as re
from colorama import Fore, Style
from sync.x2y import load_rule_records
from tqdm import tqdm

ROOT = Path(__file__).parent.parent
X_DIR = ROOT / "X"
TEXT_EXT = (".xhtml", ".opf", ".ncx")

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
colorama.init()  # 包装 stdout/stderr；非 TTY（重定向到文件/管道）时自动剥离颜色码


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


def trace_pos(original: str, seq: list[dict], i: int, pos: int) -> int | None:
    """把 pos（规则 i 应用前文本中的位置）回溯到 original 中的位置。
    位置落入前置规则改写的区间（文本是前置规则的产物）时返回 None。"""
    history = []
    text = original
    for r in seq[:i]:
        spans = []

        def repl(m_, _spans=spans, _new=r["new"]):
            _spans.append((m_.start(), m_.end(), len(m_.expand(_new))))
            return m_.expand(_new)

        text = re.sub(r["old"], repl, text)
        history.append(spans)
    for spans in reversed(history):
        shift = 0
        for s, e, nlen in spans:
            ns = s + shift  # 该替换区间在替换后文本中的起点
            if pos < ns:
                pos -= shift
                break
            if pos < ns + nlen:
                return None
            shift += nlen - (e - s)
        else:
            pos -= shift
    return pos


def first_point(vol: str, fidx: int, path: Path,
                original: str, pre: str, m, seq: list[dict], i: int) -> dict:
    """定位首个匹配：回溯到 X 原文位置；匹配文本本身是前置规则的产物时
    （落入被前置规则改写的区间），退化为规则应用时文本中的位置并加注。"""
    rel = path.relative_to(X_DIR / vol).as_posix()
    matched = m.group(0)
    s = trace_pos(original, seq, i, m.start())
    if s is None or original[s:s + len(matched)] != matched:
        base, s, note = pre, m.start(), "（位置为规则应用时文本，已被前置规则改写）"
    else:
        base, note = original, ""
    e = s + len(matched)
    line = base.count("\n", 0, s) + 1
    w = 30
    a, b = max(0, s - w), min(len(base), e + w)
    snip = re.sub(r"\s+", " ", f"{base[a:s]}{Fore.RED}{Style.BRIGHT}{matched}"
                               f"{Style.RESET_ALL}{base[e:b]}")
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
                                        re.search(seq[i]["old"], pre), seq, i)
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
    rules = load_rule_records()
    seq = ([r for r in rules if r["section"] == vol]
           + [r for r in rules if r["section"] == "*"])
    active = scan(vol, files, seq)

    head = f"范围：X/{vol}"
    if args.chapter is not None:
        head += f" 内匹配「{args.chapter}」的 {len(files)} 个文本文件"
    else:
        head += f"（{len(files)} 个文本文件）"
    print(f"{Style.BRIGHT}{head}{Style.RESET_ALL}")
    if not active:
        print("范围内无生效规则。")
        return 0
    ordered = sorted(active, key=lambda i: (active[i]["fidx"],
                                            active[i]["line"], i))
    n_vol = sum(1 for i in ordered if seq[i]["section"] == vol)
    print(f"{Style.BRIGHT}"
          f"生效规则 {len(ordered)} 条"
          f"（分卷段 {n_vol} · 通用段 {len(ordered) - n_vol}），"
          f"按首次生效点排序：{Style.RESET_ALL}\n")
    for i in ordered:
        r, p = seq[i], active[i]
        sec, sec_color = (("分卷", Fore.CYAN) if r["section"] == vol
                          else ("通用", Fore.MAGENTA))
        new = r["new"] if r["new"] else "（删除）"
        print(f"{sec_color}[{sec}:{r['lineno']}]{Style.RESET_ALL} "
              f"{Fore.RED}{r['old']}{Style.RESET_ALL} -> "
              f"{Fore.GREEN}{new}{Style.RESET_ALL}")
        print(f"  首次生效 {Fore.CYAN}{p['rel']}:{p['line']}{Style.RESET_ALL}"
              f"{Fore.YELLOW}{p['note']}{Style.RESET_ALL}")
        print(f"  {p['snip']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
