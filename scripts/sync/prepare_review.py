"""大规模 AI 审查准备：解析审查材料 → 原文纯文本 / 任务块 / 子代理提示词。

适用：triage_text 导出 .triage/review_changes.json（机器权威；TXT 仅供人读）后，
块数多、需派子代理对照日文原文审查时（.agents/skills/sync-triage/SKILL.md §6）。
产物幂等可重跑；verdicts/ 内已有 .jsonl 时拒绝重建（审查在途，先聚合或人工清理，
防任务块被换底）。

产物（.triage/ 下）：
- review_blocks.json：去重块列表（含 id，verdict 以 id 关联）
- jp_text/<卷号>.txt：../index-jp 对应卷的纯文本（剥 <rt>/标签、归并空白，
  每页一行前缀〔p-NNN〕）；index-jp 缺卷时列出并警告（报用户补充原文）
- review_chunks/task_NN.json：每任务约 50 块（同形态簇整簇不拆分装箱）；
  逐块挂载 jp_loc（bw_aligned pass 卷行号直读，定位失败块走关键词检索）
- review_prompts/task_NN.md：可直接粘贴给 delegate_task 的完整提示词

--pending：审查在途时消费 verdicts/ 已完成块 id，只对未审块重新切块
（任务编号续排、已完成任务档案保留），用于审查策略调整。

另打印重复片段形态 Top（同一术语对的批量替换可据此快速定位）。

整块差异仅为兼容字符形式（新旧文本 NFKC 等价，如全角 ＆→半角 &）的块
无需审定：自动判 ok 写入 verdicts/auto.jsonl（聚合端照常计入），不占用
子代理任务块；输出列出其片段形态供抽查。

index-jp 发布的无原文卷清单（no_original.txt）内卷豁免原文对照：对应任务的
提示词附豁免判定口径（按语料惯例与流畅度判定，jp 留空），缺卷警告仅针对
清单外卷。

用法: uv run python scripts/sync/prepare_review.py
"""

import difflib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jp_align
import verdict_io
from lib_triage import repo_root, triage_parser
from triage_text import read_review_changes

CHUNK = 50  # 每任务块数
SMALL = 20  # 小于此块数的文件参与合并
CLUSTER_MIN = 3  # 同形态块达到此数即整簇成任务（批量替换波）
JP_REPO = "../index-jp"  # 相对仓库根的位置（AGENTS.md 约定同级目录）

# 行号直读提示词（任务全部块落在 bw_aligned pass 卷且逐块定位成功时）。
# 原文已按行号对齐，无需关键词检索；行号失配是诚实失败形态，判 unlocated。
PROMPT_ALIGNED = r"""背景：你在 index-Y 仓库根目录做上游同步分流的审查。任务 JSON 的 blocks 为 [{id, files, old, new, jp_loc}]，old=旧译文，new=上游新译文（《魔禁》系列粉丝翻译修订）。绝大多数改动是正常改进，你的任务是逐块鉴定。

日文原文已与译文逐行对齐（复用上游对齐管线，门禁 0 问题）：每块的 jp_loc 列出 files 各次出现的 {file（译文文件）, line（该文件中译文行号）, jp（对应的日文行剥标签文本）, context（前后行，L行号: 文本）}。直接以 jp 行对照判定，无需检索原文。另附全卷对齐原文供扩大上下文（〔单元 L行号〕前缀，已剥 <rt> 注音和标签）：
@JP_TXT@

判定词汇（固定，不得自造新标签）：
- ok：改动正常。含：错别字修正、的地得、标点全半角、术语/译名统一（目标形式非错误形式）、语句通顺且与原文一致或更忠实的重译。
- suspect：确认有问题。特征：明显退化（改出错别字、的地误用方向反了）、重复字词/标点、混入杂字符、删字致病句或成分残缺、数字/专名改动无规律、新译与原文不符（误译、含义反转、无中生有、漏译原文信息）。
- unsure：难以判断（新旧都说得通，或对上 jp 行后仍无法确定哪个更对）。宁漏报勿误报：拿不准就 unsure 不放行；但判 suspect 与后续写规则一样须有积极证据（宁可放行上游错误改动，不错误指责上游正确改动）。
- unlocated：语义类改动但 jp 行文本与块明显无关（行号失配），reason 注明「行号失配」。

判定规则：
1. 形式级改动（错字/标点/的地得/空白）凭中文正确性直接判 ok/suspect，cls="form"，jp 留空。
2. 语义/措辞类改动（词替换、语序、整句重译、增删内容）必须对照 jp_loc 的日文行后再判，cls="semantic"，jp 填从 jp_loc 的 jp 或 context 中逐字复制的片段。通顺不等于正确；看似莫名的改动也可能是原文的忠实再现。新旧都偏离原文时，只在新译更差（错译/反义/增删信息）时判 suspect；新译更忠实或同等 → ok。语义判定必须逐块对照原文，不接受「同批同类已证实」的归纳。
3. 多文件块（jp_loc 多个出现）：逐处对照，任一处的 jp 行即可作证据。
4. 术语统一类（同一 A→B 在多块重复）：仍逐块判，但同一术语对的 jp 证据可引用同一句。

输出：逐块向 verdict 文件追加一行 JSON：{"id": <块id>, "verdict": "ok"|"suspect"|"unsure"|"unlocated", "cls": "form"|"semantic", "jp": "<原文片段，form 级留空>", "reason": "<≤30字>"}。jp 字段逐字复制 jp_loc 或对齐原文文件的文本，长句省略处以 … 分段（聚合端按 … 拆分后逐段逐字子串核验，改写字词会判为疑似编造）。每审完一批就追加写盘。禁止修改仓库中除该 verdict 文件以外的任何文件。

FINAL SUMMARY 要求：ok/suspect/unsure/unlocated 各多少；suspect/unsure/unlocated 的 id 各附一句话理由。"""

# 混合任务附加段（任务内仅部分块带 jp_loc 时）：直读块免检索，未定位块走关键词检索。
HYBRID_NOTE = """
行号直读块：任务 JSON 中带 jp_loc 字段的块已完成行号对齐定位（{file（译文文件）, line（该文件中译文行号）, jp（对应的日文行剥标签文本）, context（前后行，L行号: 文本）}）——这些块直接以 jp 行对照判定，无需关键词检索；行号失配（jp 行文本与块明显无关）判 unlocated 并注明「行号失配」。不带 jp_loc 的块按上述关键词检索方法定位。verdict 的 jp 字段：直读块从 jp_loc 或对齐原文逐字复制，检索块从分页 txt 逐字复制。全卷对齐原文供扩大上下文（〔单元 L行号〕前缀）：
@ALIGNED_TXT@"""

# 豁免原文对照的任务附加段（index-jp no_original.txt 清单内卷）。
EXEMPT_NOTE = """
豁免原文对照：以下卷经确认无法补充日文原文（index-jp no_original.txt 清单），其块的语义类改动豁免对照：@EXEMPT_VOLS@。这些块凭中文质量与语料惯例判定：有明确退化证据（错别字、病句、成分残缺、杂字符、重复字词，或违反全语料既定译名/术语统一方向）→ suspect；其余一律 → ok——新旧难以分辨优劣也判 ok，并在 reason 注明疑点存查。宁漏报勿误报：无积极证据不指责上游改动（写规则须以可坐实的退化证据为前提）。jp 留空，cls 照填，reason 写明判定依据。"""

# 批量替换簇的任务附加段（clusters 字段非空时）。
CLUSTER_NOTE = """
批量替换簇：任务 JSON 的 clusters 字段列出本任务的同形态簇（同一 diff 片段序列 = 同一批量替换波，ids 为成员）。逐簇处理：首个成员（代表块）按常规流程严格判定（语义类须定位原文）；其余成员逐块读 old/new 确认同一替换在其语境中同样成立——术语统一类通常成立，像/象这类语境敏感替换须逐块单独判断。同簇 jp 证据可共享（引用同一句原文即可）。verdict 仍逐块一行；簇内例外块单独判定并在 reason 写明。"""

# 子代理提示词模板。@JP_TXT@/@JP_TXT_FIRST@/@N@ 为占位符。
# 判定词汇与检索纪律的权威版本就在此模板内。
PROMPT = r"""背景：你在 index-Y 仓库根目录做上游同步分流的审查。任务 JSON 的 blocks 为 [{id, files, old, new}]，old=旧译文，new=上游新译文（《魔禁》系列粉丝翻译修订）。绝大多数改动是正常改进，你的任务是逐块鉴定。

日文原文纯文本（每行一页，前缀〔p-NNN〕，已剥 <rt> 注音和标签、归并空白）：
@JP_TXT@

判定词汇（固定，不得自造新标签）：
- ok：改动正常。含：错别字修正、的地得、标点全半角、术语/译名统一（目标形式非错误形式）、语句通顺且与原文一致或更忠实的重译。
- suspect：确认有问题。特征：明显退化（改出错别字、的地误用方向反了）、重复字词/标点、混入杂字符、删字致病句或成分残缺、数字/专名改动无规律、新译与原文不符（误译、含义反转、无中生有、漏译原文信息）。
- unsure：难以判断（新旧都说得通，或定位原文后仍无法确定哪个更对）。宁漏报勿误报：拿不准就 unsure 不放行；但判 suspect 与后续写规则一样须有积极证据（宁可放行上游错误改动，不错误指责上游正确改动）。
- unlocated：语义类改动但尽力后定位不到原文句。

判定规则：
1. 形式级改动（错字/标点/的地得/空白）凭中文正确性直接判 ok/suspect，cls="form"，jp 留空。
2. 语义/措辞类改动（词替换、语序、整句重译、增删内容）必须对照日文原文后再判，cls="semantic"，jp 填从 txt 复制的原文对应句片段。通顺不等于正确；看似莫名的改动也可能是原文的忠实再现。新旧都偏离原文时，只在新译更差（错译/反义/增删信息）时判 suspect；新译更忠实或同等 → ok。语义判定必须逐块对照原文，不接受「同批同类已证实」的归纳。
3. 术语统一类（同一 A→B 在多块重复）：仍逐块判，但同一术语对的 jp 证据可引用同一句。

检索原文方法（严格遵守，否则必败）：
- 【本机坑】git-bash 对非 ASCII 命令行参数按 GBK 编码，bash 里 grep 中文/日文、python -c 内联中日文都会被毁。一律用 write_file 写 .py 脚本（锚点写在脚本里），再以 uv run python <脚本绝对路径> 跑（terminal 的 workdir 设为 index-Y 仓库根目录，由运行环境提供实际路径）。
- 脚本模板：
import re
txt = open(r"@JP_TXT_FIRST@", encoding="utf-8").read()
for m in re.finditer("锚点", txt):
    print(txt[max(0, m.start()-120):m.end()+120]); print("---")
- 锚点优先选：①阿拉伯数字；②汉字专名（注意简繁/和制差异：学园→学園、滨→浜、乐→楽、丰→豊、泷→滝、壶→壺、黑→黒，不确定就多试几种）；③新旧译文共有的罕见实词。一个锚点找不到就换同句另一个。
- 批处理提效：每批 8-12 块写一个脚本同时搜所有锚点（按块打印分隔），一次跑完再逐块判定。临时脚本放 $LOCALAPPDATA/Temp 或仓库外，文件名带任务号前缀（如 t06_find.py）——并发子代理同名文件会互相覆盖。

输出：逐块向 verdict 文件追加一行 JSON：{"id": <块id>, "verdict": "ok"|"suspect"|"unsure"|"unlocated", "cls": "form"|"semantic", "jp": "<原文片段，form 级留空>", "reason": "<≤30字>"}。jp 字段逐字复制 txt 原文，长句省略处以 … 分段（聚合端按 … 拆分后逐段逐字子串核验，改写字词会判为疑似编造）。每审完一批就追加写盘。禁止修改仓库中除该 verdict 文件以外的任何文件。

FINAL SUMMARY 要求：ok/suspect/unsure/unlocated 各多少；suspect/unsure/unlocated 的 id 各附一句话理由。"""


def parse_blocks(triage: str) -> list[dict[str, Any]]:
    """review_changes.json → 机器权威块列表；TXT 仅供人工阅读。"""
    return read_review_changes(triage)


class TextOnly(HTMLParser):
    """剥 <rt> 注音与其余标签的纯文本提取器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "rt":
            self.skip += 1

    def handle_endtag(self, tag):
        if tag == "rt" and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def page_text(path: str) -> str:
    p = TextOnly()
    with open(path, encoding="utf-8") as f:
        p.feed(f.read())
    return re.sub(r"\s+", "", "".join(p.parts))


def vol_code(rel_file: str) -> str:
    code = jp_align.vol_code(rel_file)
    assert code is not None, f"路径提取不到卷码: {rel_file}"
    return code


def aligned_vols(root: str) -> dict[str, str]:
    """index-jp bw_aligned 管线 pass 卷：卷码 → 对齐单元目录（行号与译文一一对应）。"""
    return {
        v: str(p)
        for v, p in jp_align.aligned_vols(
            Path(os.path.normpath(os.path.join(root, JP_REPO)))
        ).items()
    }


_XLINES_CACHE: dict[str, list[str]] = {}
_JPLINES_CACHE: dict[tuple[str, str], list[str] | None] = {}


def locate_block(root: str, xdirs: dict, aligned: dict, b: dict) -> list[dict] | None:
    """为块的每个 files 出现定位 (file, line, jp, context)；任一处失败返回 None。

    定位锚：块 new 文本的最长非空行（≥8 字）在 X 译文文件剥标签行中的唯一命中；
    未命中时退用 old 文本（本仓库规则改过的块在 X 侧是规则前的文本形态）。
    xlines/jplines 按文件缓存（全语料逐块定位时同一文件只剥标签一次）。
    """
    locs = []
    for rel in b["files"]:
        vol = vol_code(rel)
        unit = jp_align.unit_code(rel)
        xfile = os.path.join(
            root,
            "index-X",
            "EPUB",
            xdirs.get(vol, vol),
            "OEBPS",
            "Text",
            os.path.basename(rel),
        )
        if not unit or vol not in aligned:
            return None
        if xfile not in _XLINES_CACHE:
            with open(xfile, encoding="utf-8") as f:
                _XLINES_CACHE[xfile] = [
                    jp_align.strip_line(ln) for ln in f.read().splitlines()
                ]
        xlines = _XLINES_CACHE[xfile]
        key = (vol, unit)
        if key not in _JPLINES_CACHE:
            _JPLINES_CACHE[key] = jp_align.unit_stripped_lines(aligned[vol], unit)
        jplines = _JPLINES_CACHE[key]
        if jplines is None or len(xlines) != len(jplines):
            return None
        anchors = sorted(
            (
                ln
                for side in (b["new"], b["old"])
                for ln in side.split("\n")
                if len(ln.strip()) >= 8
            ),
            key=len,
            reverse=True,
        )
        hit = jp_align.locate(xlines, anchors)
        if hit is None:
            return None
        got = jp_align.jp_at(aligned[vol], unit, hit)
        if got is None:
            return None
        locs.append(
            {"file": rel, "line": hit, "jp": got["jp"], "context": got["context"]}
        )
    return locs


def build_aligned_text(root: str, vols: list[str], aligned: dict) -> dict[str, str]:
    """为 aligned 卷导出 jp_text/<卷>.aligned.txt（〔单元 L行号〕前缀剥标签文本）。"""
    out_dir = os.path.join(root, ".triage", "jp_text")
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for vol in vols:
        if vol not in aligned:
            stale = os.path.join(out_dir, f"{vol}.aligned.txt")
            if os.path.exists(stale):
                os.remove(stale)
            continue
        lines = []
        for f in sorted(os.listdir(aligned[vol])):
            if not f.endswith(".xhtml"):
                continue
            unit = os.path.splitext(f)[0]
            with open(os.path.join(aligned[vol], f), encoding="utf-8") as fh:
                for n, ln in enumerate(fh.read().splitlines(), 1):
                    t = jp_align.strip_line(ln)
                    if t:
                        lines.append(f"〔{unit} L{n:04d}〕{t}")
        paths[vol] = os.path.join(out_dir, f"{vol}.aligned.txt")
        with open(paths[vol], "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return paths


def exempt_vols(jp_root: str) -> set[str]:
    """同级 index-jp 仓库发布的无原文卷清单 no_original.txt（每行一个卷码，
    # 起注释）：清单内卷经确认无法补充原文，豁免原文对照。"""
    path = os.path.join(jp_root, "no_original.txt")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()}


def remove_jp_artifacts(out_dir: str, vol: str) -> None:
    """缺原文时禁止任何本轮前的分页/对齐产物继续充当证据。"""
    for suffix in (".txt", ".aligned.txt"):
        path = os.path.join(out_dir, f"{vol}{suffix}")
        if os.path.exists(path):
            os.remove(path)


def build_jp_text(root: str, vols: list[str]) -> tuple[dict[str, str], list[str]]:
    """为涉及的卷生成 jp_text/<卷>.txt。返回 (卷→txt 路径, 缺原文的卷)。"""
    jp_root = os.path.normpath(os.path.join(root, JP_REPO))
    available = {
        c: d
        for d in os.listdir(jp_root)
        if (c := jp_align.vol_code(d)) and os.path.isdir(os.path.join(jp_root, d))
    }
    out_dir = os.path.join(root, ".triage", "jp_text")
    os.makedirs(out_dir, exist_ok=True)
    paths, missing = {}, []
    for vol in vols:
        if vol not in available:
            missing.append(vol)
            remove_jp_artifacts(out_dir, vol)
            continue
        vdir = os.path.join(jp_root, available[vol])
        xdir = next(
            (
                os.path.join(vdir, sub)
                for sub in ("item/xhtml", "text")
                if os.path.isdir(os.path.join(vdir, sub))
            ),
            None,
        )
        if xdir is None:
            missing.append(vol)
            remove_jp_artifacts(out_dir, vol)
            continue
        lines = [
            f"〔{os.path.splitext(pg)[0]}〕{page_text(os.path.join(xdir, pg))}"
            for pg in sorted(os.listdir(xdir))
            if os.path.splitext(pg)[1] in (".xhtml", ".html")
        ]
        paths[vol] = os.path.join(out_dir, f"{vol}.txt")
        with open(paths[vol], "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return paths, missing


def block_frags(b: dict) -> tuple:
    """块的最小差异片段序列（difflib 非 equal opcode 的 (旧片段, 新片段)）。"""
    sm = difflib.SequenceMatcher(a=b["old"], b=b["new"], autojunk=False)
    return tuple(
        (b["old"][i1:i2], b["new"][j1:j2])
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )


def compat_only(b: dict) -> bool:
    """兼容规范化块：新旧文本 NFKC 等价——差异仅来自兼容字符形式
    （全角/半角、兼容标点等，如 ＆→&）。意义不变、无审定价值，自动判 ok。"""
    old, new = b["old"], b["new"]
    return old != new and unicodedata.normalize("NFKC", old) == unicodedata.normalize(
        "NFKC", new
    )


def frag_top(blocks: list[dict], n: int = 15) -> None:
    """打印重复片段形态 Top（识别批量术语替换波次，供编排参考）。"""
    counter = Counter(block_frags(b) for b in blocks)
    print(f"\n去重片段形态 {len(counter)} 种，重复形态 Top：")
    for fs, c in counter.most_common(n):
        if c < 2:
            break
        frag = " | ".join(f"{a!r}→{x!r}" for a, x in fs)
        print(f"  [{c}块] {frag[:120]}")


def chunk_by_file(blocks: list[dict]) -> list[list[dict]]:
    """按文件切块；<SMALL 块的小文件按卷合并，余量合一组。所有组合均 ≤CHUNK。"""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for b in blocks:
        by_file[b["files"][0]].append(b)
    groups = []
    small_files = {f for f, bs in by_file.items() if len(bs) < SMALL}
    for f in sorted(by_file):
        if f in small_files:
            continue
        bs = by_file[f]
        for k in range(0, len(bs), CHUNK):
            groups.append(bs[k : k + CHUNK])
    by_vol: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(small_files):
        by_vol[vol_code(f)].extend(by_file[f])
    rest = []
    for vol, bs in sorted(by_vol.items()):
        if len(bs) >= SMALL:
            for k in range(0, len(bs), CHUNK):
                groups.append(bs[k : k + CHUNK])
        else:
            rest.extend(bs)
    for k in range(0, len(rest), CHUNK):
        groups.append(rest[k : k + CHUNK])
    return groups


def chunk(blocks: list[dict]) -> list[list[dict]]:
    """切块：同形态簇（≥CLUSTER_MIN 块共享同一 diff 片段序列 = 同一批量
    替换波）整簇不拆分；簇按大到小 first-fit 装箱（每任务 ≤CHUNK，一任务
    可多簇——clusters 字段由任务内实际块重建，提示词逐簇处理不受影响），
    超 CHUNK 的簇独占任务并切分；非簇散块按文件切块。

    装箱动机：子代理固定开销（提示词+语料+自检）按任务计，小簇各自独立
    成任务会按簇数放大额度消耗；装箱把固定开销摊到更多块上。
    """
    by_shape: dict[tuple, list[dict]] = defaultdict(list)
    for b in blocks:
        by_shape[block_frags(b)].append(b)
    groups = []
    small = []
    packable = []
    for _shape, bs in sorted(by_shape.items(), key=lambda kv: -len(kv[1])):
        if len(bs) < CLUSTER_MIN:
            small.extend(bs)
        elif len(bs) > CHUNK:
            for k in range(0, len(bs), CHUNK):  # 超 CHUNK 的簇被迫切分
                groups.append(bs[k : k + CHUNK])
        else:
            packable.append(bs)
    bins: list[list[dict]] = []
    room: list[int] = []
    for bs in packable:  # 已按簇大到小排序（first-fit decreasing）
        for i, r in enumerate(room):
            if r >= len(bs):
                bins[i].extend(bs)
                room[i] -= len(bs)
                break
        else:
            bins.append(list(bs))
            room.append(CHUNK - len(bs))
    groups.extend(bins)
    groups.extend(chunk_by_file(small))
    return groups


def write_task(
    root: str,
    triage: str,
    n: int,
    bs: list[dict],
    xdirs: dict,
    aligned: dict,
    aligned_paths: dict[str, str],
    jp_paths: dict[str, str],
    missing_exempt: list[str],
) -> int:
    """写单个审查任务的 chunk JSON + 提示词；返回本任务 jp_loc 挂载块数。

    逐块挂 jp_loc（行号直读）：全定位 = 纯直读提示词；部分定位 = 混合
    （HYBRID_NOTE）；零定位 = 关键词检索提示词。chunk/prompt 目录下对应
    task_NN 旧文件被覆写；verdict 目录只拼路径不写文件。
    """
    chunk_dir = os.path.join(triage, "review_chunks")
    prompt_dir = os.path.join(triage, "review_prompts")
    verdict_dir = os.path.join(triage, "verdicts")
    vols_n = sorted({vol_code(f) for b in bs for f in b["files"]})
    files = sorted({f for b in bs for f in b["files"]})
    shapes_in: dict[tuple, list[int]] = defaultdict(list)
    for b in bs:
        shapes_in[block_frags(b)].append(b["id"])
    clusters = [
        {"shape": [[a, x] for a, x in s], "ids": ids}
        for s, ids in shapes_in.items()
        if len(ids) >= CLUSTER_MIN
    ]
    n_loc = 0
    for b in bs:
        loc = locate_block(root, xdirs, aligned, b)
        if loc is not None:
            b["jp_loc"] = loc
            n_loc += 1
    task_json = os.path.join(chunk_dir, f"task_{n:02d}.json")
    out_jsonl = os.path.join(verdict_dir, f"task_{n:02d}.jsonl")
    with open(task_json, "w", encoding="utf-8") as f:
        json.dump(
            {"vols": vols_n, "files": files, "blocks": bs, "clusters": clusters},
            f,
            ensure_ascii=False,
            indent=1,
        )
    if n_loc == len(bs):
        jp_lines = "\n".join(
            f"- {aligned_paths[v]}" for v in vols_n if v in aligned_paths
        )
        prompt = PROMPT_ALIGNED
    else:
        jp_lines = "\n".join(f"- {jp_paths[v]}" for v in vols_n if v in jp_paths)
        prompt = PROMPT
        if n_loc:
            prompt += HYBRID_NOTE.replace(
                "@ALIGNED_TXT@",
                "\n".join(
                    f"- {aligned_paths[v]}" for v in vols_n if v in aligned_paths
                ),
            )
    ex_n = [v for v in vols_n if v in missing_exempt]
    if ex_n:
        prompt += EXEMPT_NOTE.replace("@EXEMPT_VOLS@", "、".join(ex_n))
    if clusters:
        prompt += CLUSTER_NOTE
    prompt += (
        f"\n\n任务：审查 {task_json} 的 @N@ 个块，verdict 逐块写入 "
        f"{out_jsonl}（UTF-8 每块一行），全部审完后自检：行数==@N@、"
        "每行 JSON 可解析、id 无重复无遗漏。"
    )
    prompt = (
        prompt.replace("@JP_TXT@", jp_lines)
        .replace("@JP_TXT_FIRST@", jp_paths.get(vols_n[0], "<缺原文卷>"))
        .replace("@N@", str(len(bs)))
    )
    with open(os.path.join(prompt_dir, f"task_{n:02d}.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    cl = "、".join(str(len(c["ids"])) for c in clusters)
    print(
        f"task_{n:02d} [{'/'.join(vols_n)}] {len(bs)}块"
        + (
            "（行号直读）"
            if n_loc == len(bs)
            else (f"（直读 {n_loc}/{len(bs)}）" if n_loc else "")
        )
        + (f"（簇 {cl}）" if cl else "")
    )
    return n_loc


def clear_task_outputs(triage: str) -> None:
    for name in ("review_chunks", "review_prompts", "verdicts"):
        directory = os.path.join(triage, name)
        os.makedirs(directory, exist_ok=True)
        for old in os.listdir(directory):
            os.remove(os.path.join(directory, old))


def main() -> int:
    parser = triage_parser(__doc__)
    parser.add_argument(
        "--pending",
        action="store_true",
        help="在途审查中重建：消费 verdicts/ 已完成块，只对未审块重新切块（任务编号续排）",
    )
    args = parser.parse_args()
    root = repo_root()
    triage = os.path.join(root, ".triage")
    try:
        blocks = parse_blocks(triage)
    except FileNotFoundError:
        print("无 .triage/review_changes.json（先跑 triage_text）")
        return 1
    except RuntimeError as e:
        print(f"错误：{e}")
        return 1
    if not blocks:
        with open(
            os.path.join(triage, "review_blocks.json"), "w", encoding="utf-8"
        ) as f:
            json.dump([], f)
        clear_task_outputs(triage)
        for name in ("sample_plan.json", "sample_plan.json.tmp"):
            stale = os.path.join(triage, name)
            if os.path.exists(stale):
                os.remove(stale)
        print("审查材料 0 块，已写出空块集合并清理旧任务")
        return 0

    verdict_dir = os.path.join(triage, "verdicts")
    done_ids: set[int] = set()
    if args.pending:
        try:
            with open(
                os.path.join(triage, "review_blocks.json"), encoding="utf-8"
            ) as f:
                old_blocks = json.load(f)
            done_ids = set(verdict_io.load_verdicts(triage, old_blocks))
        except verdict_io.VerdictError as exc:
            print(f"verdict 校验失败：\n{exc}")
            return 1
    elif os.path.isdir(verdict_dir):
        for fn in os.listdir(verdict_dir):
            if not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(verdict_dir, fn), encoding="utf-8") as f:
                for ln in f:
                    if ln.strip():
                        done_ids.add(json.loads(ln)["id"])
    if done_ids and not args.pending:
        print(
            "错误：verdicts/ 已有审查产出（审查在途），拒绝重建任务块；"
            "先跑 aggregate_verdicts.py 聚合或人工清理"
            "（审查策略调整、只对未审块重建时用 --pending）"
        )
        return 1

    print(f"块总数 {len(blocks)}")

    # 兼容规范化块（NFKC 等价，如 ＆→&）自动判 ok，不占用子代理任务块
    auto = [b for b in blocks if compat_only(b)]
    review = [b for b in blocks if not compat_only(b)]
    if auto and not args.pending:  # --pending 时 auto.jsonl 已在首轮写入且 id 计入 done
        shapes = Counter(block_frags(b) for b in auto)
        top = "、".join(
            f"{' | '.join(f'{a}→{x}' for a, x in fs)}×{c}"
            for fs, c in shapes.most_common(5)
        )
        print(f"兼容规范化自动放行 {len(auto)} 块（{top}）")
    if args.pending:
        review = [b for b in review if b["id"] not in done_ids]
        print(f"--pending：已完成 {len(done_ids)} 块，待审 {len(review)} 块")

    vols = sorted({vol_code(f) for b in blocks for f in b["files"]})
    jp_paths, missing = build_jp_text(root, vols)
    for vol, p in sorted(jp_paths.items()):
        print(f"原文 {vol} -> {os.path.relpath(p, root)}")
    xdirs = {
        c: d
        for d in os.listdir(os.path.join(root, "index-X", "EPUB"))
        if (c := jp_align.vol_code(d))
        and os.path.isdir(os.path.join(root, "index-X", "EPUB", d))
    }
    exempt = exempt_vols(os.path.normpath(os.path.join(root, JP_REPO)))
    missing_real = [v for v in missing if v not in exempt]
    missing_exempt = [v for v in missing if v in exempt]
    for b in blocks:
        block_vols = {vol_code(f) for f in b["files"]}
        b["jp_exempt"] = bool(block_vols) and all(
            v in missing_exempt for v in block_vols
        )
    with open(os.path.join(triage, "review_blocks.json"), "w", encoding="utf-8") as f:
        json.dump(blocks, f, ensure_ascii=False, indent=1)
    if missing_exempt:
        print(
            f"缺原文卷（已确认无法补充，豁免对照）："
            f"{[xdirs.get(v, v) for v in missing_exempt]}"
        )
    if missing_real:
        print(
            f"警告：index-jp 缺原文卷 {[xdirs.get(v, v) for v in missing_real]}，"
            "相关块须报用户补充原文后核实"
            "（.agents/skills/sync-triage/SKILL.md §6 对照规则）"
        )

    groups = chunk(review)
    aligned = {
        vol: path for vol, path in aligned_vols(root).items() if vol not in missing
    }
    aligned_paths = build_aligned_text(root, vols, aligned)
    for vol, p in sorted(aligned_paths.items()):
        print(f"对齐原文 {vol} -> {os.path.relpath(p, root)}")
    chunk_dir = os.path.join(triage, "review_chunks")
    prompt_dir = os.path.join(triage, "review_prompts")
    start_n = 0
    if args.pending:  # 续排编号，保留已完成任务的 chunk/prompt/verdict 档案
        for d in (chunk_dir, prompt_dir, verdict_dir):
            os.makedirs(d, exist_ok=True)
        prev = [
            int(m.group(1))
            for f in os.listdir(chunk_dir)
            if (m := re.match(r"task_(\d+)\.json$", f))
        ]
        start_n = max(prev) + 1 if prev else 0
    else:
        clear_task_outputs(triage)
    if auto and not args.pending:
        with open(os.path.join(verdict_dir, "auto.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(
                json.dumps(
                    {
                        "id": b["id"],
                        "verdict": "ok",
                        "cls": "form",
                        "jp": "",
                        "reason": "auto: NFKC 等价（兼容字符形式差异）",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for b in auto
            )
    n_loc_all = 0
    for n, bs in enumerate(groups, start=start_n):
        n_loc_all += write_task(
            root,
            triage,
            n,
            bs,
            xdirs,
            aligned,
            aligned_paths,
            jp_paths,
            missing_exempt,
        )
    n_blocks = sum(len(g) for g in groups)
    print(
        f"行号直读挂载：{n_loc_all}/{n_blocks} 块（{n_loc_all / max(n_blocks, 1):.1%}）",
    )
    print(
        f"\n提示词已写入 {os.path.relpath(prompt_dir, root)}/"
        "（delegate_task 每任务粘贴一份）"
    )
    frag_top(review)
    return 0


if __name__ == "__main__":
    sys.exit(main())
