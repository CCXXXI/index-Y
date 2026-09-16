"""大规模 AI 审查准备：解析审查材料 → 原文纯文本 / 任务块 / 子代理提示词。

适用：triage_text 导出 .triage/review_changes.txt 后，块数多、需派子代理
对照日文原文审查时（docs/sync-triage.md §6）。产物幂等可重跑；verdicts/
内已有 .jsonl 时拒绝重建（审查在途，先聚合或人工清理，防任务块被换底）。

产物（.triage/ 下）：
- review_blocks.json：去重块列表（含 id，verdict 以 id 关联）
- jp_text/<卷号>.txt：../index-jp 对应卷的纯文本（剥 <rt>/标签、归并空白，
  每页一行前缀〔p-NNN〕）；index-jp 缺卷时列出并警告（报用户补充原文）
- review_chunks/task_NN.json：每任务约 50 块；<20 块的小文件按卷合并成组
- review_prompts/task_NN.md：可直接粘贴给 delegate_task 的完整提示词

另打印重复片段形态 Top（同一术语对的批量替换可据此快速定位）。

用法: uv run python scripts/sync/prepare_review.py
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from html.parser import HTMLParser
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import repo_root, triage_parser

CHUNK = 50          # 每任务块数
SMALL = 20          # 小于此块数的文件参与合并
JP_REPO = "../../index-jp"  # 相对仓库根的位置（AGENTS.md 约定同级目录）

# 子代理提示词模板。@JP_TXT@/@JP_TXT_FIRST@/@N@ 为占位符。
# 判定词汇与检索纪律的权威版本就在此模板内。
PROMPT = r"""背景：你在 index-Y 仓库（C:\Users\ccxxx\Documents\GitHub\index-Y）做上游同步分流的审查。任务 JSON 的 blocks 为 [{id, files, old, new}]，old=旧译文，new=上游新译文（《魔禁》系列粉丝翻译修订）。绝大多数改动是正常改进，你的任务是逐块鉴定。

日文原文纯文本（每行一页，前缀〔p-NNN〕，已剥 <rt> 注音和标签、归并空白）：
@JP_TXT@

判定词汇（固定，不得自造新标签）：
- ok：改动正常。含：错别字修正、的地得、标点全半角、术语/译名统一（目标形式非错误形式）、语句通顺且与原文一致或更忠实的重译。
- suspect：确认有问题。特征：明显退化（改出错别字、的地误用方向反了）、重复字词/标点、混入杂字符、删字致病句或成分残缺、数字/专名改动无规律、新译与原文不符（误译、含义反转、无中生有、漏译原文信息）。
- unsure：难以判断（新旧都说得通，或定位原文后仍无法确定哪个更对）。宁漏勿错：拿不准就 unsure，不要放行。
- unlocated：语义类改动但尽力后定位不到原文句。

判定规则：
1. 形式级改动（错字/标点/的地得/空白）凭中文正确性直接判 ok/suspect，cls="form"，jp 留空。
2. 语义/措辞类改动（词替换、语序、整句重译、增删内容）必须对照日文原文后再判，cls="semantic"，jp 填从 txt 复制的原文对应句片段。通顺不等于正确；看似莫名的改动也可能是原文的忠实再现。新旧都偏离原文时，只在新译更差（错译/反义/增删信息）时判 suspect；新译更忠实或同等 → ok。语义判定必须逐块对照原文，不接受「同批同类已证实」的归纳。
3. 术语统一类（同一 A→B 在多块重复）：仍逐块判，但同一术语对的 jp 证据可引用同一句。

检索原文方法（严格遵守，否则必败）：
- 【本机坑】git-bash 对非 ASCII 命令行参数按 GBK 编码，bash 里 grep 中文/日文、python -c 内联中日文都会被毁。一律用 write_file 写 .py 脚本（锚点写在脚本里），再以 uv run python <脚本绝对路径> 跑（terminal 的 workdir 设为 C:\Users\ccxxx\Documents\GitHub\index-Y）。
- 脚本模板：
import re
txt = open(r"@JP_TXT_FIRST@", encoding="utf-8").read()
for m in re.finditer("锚点", txt):
    print(txt[max(0, m.start()-120):m.end()+120]); print("---")
- 锚点优先选：①阿拉伯数字；②汉字专名（注意简繁/和制差异：学园→学園、滨→浜、乐→楽、丰→豊、泷→滝、壶→壺、黑→黒，不确定就多试几种）；③新旧译文共有的罕见实词。一个锚点找不到就换同句另一个。
- 批处理提效：每批 8-12 块写一个脚本同时搜所有锚点（按块打印分隔），一次跑完再逐块判定。

输出：逐块向 verdict 文件追加一行 JSON：{"id": <块id>, "verdict": "ok"|"suspect"|"unsure"|"unlocated", "cls": "form"|"semantic", "jp": "<原文片段，form 级留空>", "reason": "<≤30字>"}。每审完一批就追加写盘。禁止修改仓库中除该 verdict 文件以外的任何文件。

FINAL SUMMARY 要求：ok/suspect/unsure/unlocated 各多少；suspect/unsure/unlocated 的 id 各附一句话理由。"""


def parse_blocks(triage: str) -> list[dict[str, Any]]:
    """review_changes.txt → [{id, files, old, new}]（old/new 取 -/+ 行拼接）。"""
    blocks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    with open(os.path.join(triage, "review_changes.txt"), encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\[(\d+)次\] (.+)$", line.rstrip("\n"))
            if m:
                if cur:
                    blocks.append(cur)
                cur = {"files": [x.strip() for x in m.group(2).split("、")],
                       "old": "", "new": ""}
            elif line.startswith("- ") and cur is not None:
                cur["old"] += line[2:].rstrip("\n")
            elif line.startswith("+ ") and cur is not None:
                cur["new"] += line[2:].rstrip("\n")
    if cur:
        blocks.append(cur)
    for i, b in enumerate(blocks):
        b["id"] = i
    return blocks


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
    return rel_file.split("]")[0].strip("[")


def build_jp_text(root: str, vols: list[str]) -> tuple[dict[str, str], list[str]]:
    """为涉及的卷生成 jp_text/<卷>.txt。返回 (卷→txt 路径, 缺原文的卷)。"""
    jp_root = os.path.normpath(os.path.join(root, JP_REPO))
    available = {d.split("]")[0].strip("["): d for d in os.listdir(jp_root)
                 if d.startswith("[") and os.path.isdir(os.path.join(jp_root, d))}
    out_dir = os.path.join(root, ".triage", "jp_text")
    os.makedirs(out_dir, exist_ok=True)
    paths, missing = {}, []
    for vol in vols:
        if vol not in available:
            missing.append(vol)
            continue
        xdir = os.path.join(jp_root, available[vol], "item", "xhtml")
        lines = [f"〔{pg[:-6]}〕{page_text(os.path.join(xdir, pg))}"
                 for pg in sorted(os.listdir(xdir))]
        paths[vol] = os.path.join(out_dir, f"{vol}.txt")
        with open(paths[vol], "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return paths, missing


def frag_top(blocks: list[dict], n: int = 15) -> None:
    """打印重复片段形态 Top（识别批量术语替换波次，供编排参考）。"""
    import difflib
    counter = Counter()
    for b in blocks:
        sm = difflib.SequenceMatcher(a=b["old"], b=b["new"], autojunk=False)
        fs = tuple((b["old"][i1:i2], b["new"][j1:j2])
                   for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
        counter[fs] += 1
    print(f"\n去重片段形态 {len(counter)} 种，重复形态 Top：")
    for fs, c in counter.most_common(n):
        if c < 2:
            break
        frag = " | ".join(f"{a!r}→{x!r}" for a, x in fs)
        print(f"  [{c}块] {frag[:120]}")


def chunk(blocks: list[dict]) -> list[list[dict]]:
    """按文件切块（每块 ≤CHUNK）；<SMALL 块的小文件按卷合并，余量合一组。"""
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
            groups.append(bs[k:k + CHUNK])
    by_vol: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(small_files):
        by_vol[vol_code(f)].extend(by_file[f])
    rest = []
    for vol, bs in sorted(by_vol.items()):
        if len(bs) >= SMALL:
            groups.append(bs)
        else:
            rest.extend(bs)
    if rest:
        groups.append(rest)
    return groups


def main() -> int:
    triage_parser(__doc__).parse_args()
    root = repo_root()
    triage = os.path.join(root, ".triage")
    if not os.path.exists(os.path.join(triage, "review_changes.txt")):
        print("无 .triage/review_changes.txt（先跑 triage_text）")
        return 1

    verdict_dir = os.path.join(triage, "verdicts")
    if os.path.isdir(verdict_dir) and any(
            f.endswith(".jsonl") for f in os.listdir(verdict_dir)):
        print("错误：verdicts/ 已有审查产出（审查在途），拒绝重建任务块；"
              "先跑 aggregate_verdicts.py 聚合或人工清理")
        return 1

    blocks = parse_blocks(triage)
    if not blocks:
        print("审查材料 0 块，无需准备")
        return 0
    with open(os.path.join(triage, "review_blocks.json"), "w",
              encoding="utf-8") as f:
        json.dump(blocks, f, ensure_ascii=False, indent=1)
    print(f"块总数 {len(blocks)}")

    vols = sorted({vol_code(b["files"][0]) for b in blocks})
    jp_paths, missing = build_jp_text(root, vols)
    for vol, p in sorted(jp_paths.items()):
        print(f"原文 {vol} -> {os.path.relpath(p, root)}")
    if missing:
        print(f"警告：index-jp 缺原文卷 {missing}，相关块须报用户补充原文后核实"
              "（docs/sync-triage.md §6 对照规则）")

    groups = chunk(blocks)
    chunk_dir = os.path.join(triage, "review_chunks")
    prompt_dir = os.path.join(triage, "review_prompts")
    for d in (chunk_dir, prompt_dir, verdict_dir):
        os.makedirs(d, exist_ok=True)
        for old in os.listdir(d):
            os.remove(os.path.join(d, old))
    for n, bs in enumerate(groups):
        vols_n = sorted({vol_code(b["files"][0]) for b in bs})
        files = sorted({f for b in bs for f in b["files"]})
        task_json = os.path.join(chunk_dir, f"task_{n:02d}.json")
        out_jsonl = os.path.join(verdict_dir, f"task_{n:02d}.jsonl")
        with open(task_json, "w", encoding="utf-8") as f:
            json.dump({"vols": vols_n, "files": files, "blocks": bs},
                      f, ensure_ascii=False, indent=1)
        jp_lines = "\n".join(f"- {jp_paths[v]}" for v in vols_n if v in jp_paths)
        prompt = PROMPT + (
            f"\n\n任务：审查 {task_json} 的 @N@ 个块，verdict 逐块写入 "
            f"{out_jsonl}（UTF-8 每块一行），全部审完后自检：行数==@N@、"
            "每行 JSON 可解析、id 无重复无遗漏。")
        prompt = (prompt
                  .replace("@JP_TXT@", jp_lines)
                  .replace("@JP_TXT_FIRST@", jp_paths.get(vols_n[0], "<缺原文卷>"))
                  .replace("@N@", str(len(bs))))
        with open(os.path.join(prompt_dir, f"task_{n:02d}.md"), "w",
                  encoding="utf-8") as f:
            f.write(prompt)
        print(f"task_{n:02d} [{'/'.join(vols_n)}] {len(bs)}块")
    print(f"\n提示词已写入 {os.path.relpath(prompt_dir, root)}/"
          "（delegate_task 每任务粘贴一份，建议并发 ≤6）")
    frag_top(blocks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
