"""验收抽样审查：上游已多轮复核前提下的二校降本。

三层分流无 verdict 的待审块：
1. 机械排雷（删除内容/两侧拉丁数字不一致/新增重复字词）→ 全量深审
2. 子波（块末次归属 commit × 卷 × 文件）验收抽样：≥15 块抽 8（id 等距
   分层），5-14 块抽 4；--apply 时样本 ≥1 suspect 或 ≥2 非 ok → 该子波
   全量深审（升级），通过则覆盖外块标 auto 放行（verdicts/auto_sample.jsonl，
   聚合时与 LLM-ok 分列）
3. <5 块子波与无归属块全量深审（异质长尾，风险集中）

--plan：生成抽样任务（编号续排）+ sample_plan.json，并清理被抽样方案
  接管的旧待审任务文件（无 verdict 的 task_* chunk/prompt）
--apply：消费抽样 verdict → 逐子波升级（追加全量任务）或放行（写 auto_sample）
用法: uv run python scripts/sync/sample_review.py --plan | --apply
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict

import regex

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import jp_align
import prepare_review as pr
import verdict_io
from lib_triage import repo_root, triage_parser

K_BIG, K_MID = 8, 4  # 子波 ≥15 块抽 8；5-14 块抽 4；<5 全量
ALNUM = re.compile(r"[^0-9A-Za-z]+")
DUP2 = re.compile(r"([一-鿿぀-ヿ]{2})\1")
DUP1 = re.compile(r"([一-鿿぀-ヿ])\1")
DUP_PUNCT = regex.compile(r"(\p{P})\1")


def red_flag(b: dict) -> str | None:
    """机械红旗（宁多勿漏，误报由深审吸收）：删除内容/拉丁数字变动/新增重复字词。"""
    old, new = b["old"], b["new"]
    if old.strip() and not new.strip():
        return "delete"
    if ALNUM.sub("", old) != ALNUM.sub("", new):
        return "alnum"
    for pattern in (DUP2, DUP1, DUP_PUNCT):
        for m in pattern.finditer(new):
            repeated = m.group(0)
            if new.count(repeated) > old.count(repeated):
                return "dup"
    return None


def load_star(triage: str, n_blocks: int) -> list[list[str]]:
    """从结构化 review_changes.json 读取逐块 commit 归属（与块 id 同序）。"""
    with open(os.path.join(triage, "review_changes.json"), encoding="utf-8") as f:
        changes = json.load(f)
    stars = [list(change["commits"]) for change in changes]
    assert len(stars) == n_blocks, f"commit 对齐失败：{len(stars)} != {n_blocks}"
    return stars


def load_done(triage: str, blocks: list[dict]) -> set[int]:
    return set(verdict_io.load_verdicts(triage, blocks))


def stratified(ids: list[int], k: int) -> list[int]:
    """id 升序等距取 k 个（确定性，无种子）。"""
    if k >= len(ids):
        return list(ids)
    return sorted({ids[round(i * (len(ids) - 1) / (k - 1))] for i in range(k)})


def next_task_n(triage: str) -> int:
    cdir = os.path.join(triage, "review_chunks")
    prev = [
        int(m.group(1))
        for f in os.listdir(cdir)
        if (m := re.match(r"task_(\d+)\.json$", f))
    ]
    return max(prev) + 1 if prev else 0


def task_assignments(triage: str, task_ids: list[int]) -> set[int]:
    """仅读取 sample_plan 本轮抽样/升级任务；缺失文件留待重跑补建。"""
    cdir = os.path.join(triage, "review_chunks")
    assigned: set[int] = set()
    for task_id in task_ids:
        path = os.path.join(cdir, f"task_{task_id:02d}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            task = json.load(f)
        assigned.update(b["id"] for b in task["blocks"])
    return assigned


def save_plan(triage: str, plan_doc: dict) -> None:
    """原子持久化任务分配，避免崩溃后把在途升级误判为旧归档。"""
    path = os.path.join(triage, "sample_plan.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan_doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def plan() -> int:
    root = repo_root()
    triage = os.path.join(root, ".triage")
    with open(os.path.join(triage, "review_blocks.json"), encoding="utf-8") as f:
        blocks = json.load(f)
    try:
        done = load_done(triage, blocks)
    except verdict_io.VerdictError as exc:
        print(f"verdict 校验失败：\n{exc}")
        return 1
    stars = load_star(triage, len(blocks))
    pending = [b for b in blocks if b["id"] not in done]
    print(f"待审 {len(pending)} 块（已完成 {len(done)}）")

    # 机械排雷
    flagged: dict[int, str] = {}
    for b in pending:
        r = red_flag(b)
        if r:
            flagged[b["id"]] = r
    print(f"机械排雷命中 {len(flagged)} 块：{dict(Counter(flagged.values()))}")

    # 子波分组：末次归属 commit × 卷 × 文件
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for b in pending:
        commits = [c for c in stars[b["id"]] if c != "NONE"]
        key = (
            commits[-1] if commits else "NONE",
            pr.vol_code(b["files"][0]),
            b["files"][0],
        )
        groups[key].append(b["id"])

    sampled: dict[tuple, list[int]] = {}
    deferred: dict[tuple, list[int]] = {}
    review: set[int] = set(flagged)
    for key, ids in sorted(groups.items()):
        ids = sorted(ids)
        if key[0] == "NONE" or len(ids) < 5:
            take = list(ids)  # 无归属与 <5 块子波全量
        else:
            take = stratified(ids, K_BIG if len(ids) >= 15 else K_MID)
        sampled[key] = take
        deferred[key] = [i for i in ids if i not in take and i not in flagged]
        review.update(take)
    print(
        f"子波 {len(groups)} 个：抽样 {sum(len(v) for v in sampled.values())} 块，"
        f"覆盖外待定 {sum(len(v) for v in deferred.values())} 块，"
        f"进深审合计 {len(review)} 块"
    )

    # 旧待审任务文件清理（无 verdict 的 chunk/prompt 对被抽样方案接管）
    cdir = os.path.join(triage, "review_chunks")
    pdir = os.path.join(triage, "review_prompts")
    vdir = os.path.join(triage, "verdicts")
    removed = 0
    for fn in os.listdir(cdir):
        m = re.match(r"task_(\d+)\.json$", fn)
        if not m:
            continue
        vf = os.path.join(vdir, f"task_{m.group(1)}.jsonl")
        if not os.path.exists(vf) or os.path.getsize(vf) == 0:
            os.remove(os.path.join(cdir, fn))
            pf = os.path.join(pdir, f"task_{m.group(1)}.md")
            if os.path.exists(pf):
                os.remove(pf)
            removed += 1
    print(f"清理在途旧任务文件 {removed} 对")

    # 写抽样任务（按 卷/文件/id 排序保持线性，50 块/任务，编号续排）
    by_id = {b["id"]: b for b in blocks}
    sel = sorted(
        review,
        key=lambda i: (pr.vol_code(by_id[i]["files"][0]), by_id[i]["files"][0], i),
    )
    xdirs = {
        c: d
        for d in os.listdir(os.path.join(root, "index-X", "EPUB"))
        if (c := jp_align.vol_code(d))
        and os.path.isdir(os.path.join(root, "index-X", "EPUB", d))
    }
    vols = sorted({pr.vol_code(f) for b in blocks for f in b["files"]})
    jp_paths, _missing = pr.build_jp_text(root, vols)
    aligned = {
        vol: path for vol, path in pr.aligned_vols(root).items() if vol not in _missing
    }
    aligned_paths = pr.build_aligned_text(root, vols, aligned)
    exempt = pr.exempt_vols(os.path.normpath(os.path.join(root, pr.JP_REPO)))
    missing_exempt = [v for v in _missing if v in exempt]
    n = next_task_n(triage)
    n_loc = 0
    tasks = []
    for k in range(0, len(sel), pr.CHUNK):
        bs = [by_id[i] for i in sel[k : k + pr.CHUNK]]
        n_loc += pr.write_task(
            root, triage, n, bs, xdirs, aligned, aligned_paths, jp_paths, missing_exempt
        )
        tasks.append(n)
        n += 1
    print(
        f"抽样任务 {len(tasks)} 个（task_{tasks[0]:02d}..task_{tasks[-1]:02d}），"
        f"jp_loc 挂载 {n_loc}/{len(sel)}"
        if tasks
        else "无抽样任务"
    )

    with open(os.path.join(triage, "sample_plan.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "k": {"big": K_BIG, "mid": K_MID},
                "groups": [
                    {
                        "key": list(key),
                        "total": len(sampled[key]) + len(deferred[key]),
                        "sampled": sampled[key],
                        "deferred": deferred[key],
                    }
                    for key in sorted(groups)
                ],
                "flagged": flagged,
                "tasks": tasks,
                "escalation_tasks": [],
            },
            f,
            ensure_ascii=False,
            indent=1,
        )
    print("sample_plan.json 已写入；--apply 在抽样 verdict 齐后执行升级/放行")
    return 0


def apply() -> int:
    root = repo_root()
    triage = os.path.join(root, ".triage")
    with open(os.path.join(triage, "review_blocks.json"), encoding="utf-8") as f:
        blocks = json.load(f)
    if not blocks:
        print("审查材料 0 块，无需 apply")
        return 0
    with open(os.path.join(triage, "sample_plan.json"), encoding="utf-8") as f:
        plan_doc = json.load(f)
    by_id = {b["id"]: b for b in blocks}
    vdir = os.path.join(triage, "verdicts")
    vols = sorted({pr.vol_code(f) for b in blocks for f in b["files"]})
    jp_paths, missing_original = pr.build_jp_text(root, vols)
    aligned = {
        vol: path
        for vol, path in pr.aligned_vols(root).items()
        if vol not in missing_original
    }
    aligned_paths = pr.build_aligned_text(root, vols, aligned)
    exempt = pr.exempt_vols(os.path.normpath(os.path.join(root, pr.JP_REPO)))
    missing_exempt = [v for v in missing_original if v in exempt]
    try:
        verdicts = verdict_io.load_verdicts(triage, blocks)
    except verdict_io.VerdictError as exc:
        print(f"verdict 校验失败：\n{exc}")
        return 1

    # 排雷块的子波归属（plan 时未记入 sampled/deferred，--apply 重算并入判定）
    stars = load_star(triage, len(blocks))
    flag_map: dict[tuple, list[int]] = defaultdict(list)
    for fid_str in plan_doc["flagged"]:
        fid = int(fid_str)
        b = by_id[fid]
        commits = [c for c in stars[fid] if c != "NONE"]
        key = (
            commits[-1] if commits else "NONE",
            pr.vol_code(b["files"][0]),
            b["files"][0],
        )
        flag_map[key].append(fid)

    assigned = task_assignments(
        triage,
        [*plan_doc.get("tasks", []), *plan_doc.get("escalation_tasks", [])],
    )

    # 升级判定：样本+本子波排雷块的 verdict；≥1 suspect 或 ≥2 非 ok → 全量深审
    escalate: list[tuple] = []
    auto_lines: dict[int, str] = {}
    missing: dict[tuple, list[int]] = {}
    revoked_auto: set[int] = set()
    for g in plan_doc["groups"]:
        key = tuple(g["key"])
        if not g["deferred"]:
            continue  # 全量子波无放行项
        judged = sorted(
            {i for i in g["sampled"] + flag_map.get(key, []) if i in verdicts}
        )
        lack = sorted(
            {i for i in g["sampled"] + flag_map.get(key, []) if i not in verdicts}
        )
        if lack:
            missing[key] = lack
            continue
        if all(i in verdicts and verdicts[i]["cls"] != "auto" for i in g["deferred"]):
            continue  # 升级块已全有实名 verdict，不再因原样本结论空转
        non_ok = [i for i in judged if verdicts[i]["verdict"] != "ok"]
        n_sus = sum(1 for i in judged if verdicts[i]["verdict"] == "suspect")
        has_pending_assignment = any(
            i not in verdicts and i in assigned for i in g["deferred"]
        )
        if n_sus >= 1 or len(non_ok) >= 2 or has_pending_assignment:
            escalate.append(key)
            revoked_auto.update(
                i
                for i in g["deferred"]
                if i in verdicts and verdicts[i]["cls"] == "auto"
            )
            continue
        if all(i in verdicts for i in g["deferred"]):
            continue  # 覆盖外块已全有 verdict（升级完成后重跑 --apply 的情形）
        for i in g["deferred"]:
            if i in verdicts:  # 已被其他途径审过（排雷/重叠），不覆写
                continue
            auto_lines[i] = (
                json.dumps(
                    {
                        "id": i,
                        "verdict": "ok",
                        "cls": "auto",
                        "jp": "",
                        "reason": f"auto: 验收抽样覆盖外（子波 {key[0]}/{key[1]} "
                        f"{os.path.basename(key[2])} 样本通过）",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    if missing:
        print(f"以下子波样本/排雷 verdict 未齐，未放行：{len(missing)} 个")
        for key, ids in list(missing.items())[:10]:
            print(f"  {key[0]}/{key[1]}/{os.path.basename(key[2])}: 缺 {ids}")
    if revoked_auto:
        path = os.path.join(vdir, "auto_sample.jsonl")
        kept = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip() and json.loads(line)["id"] not in revoked_auto:
                    kept.append(line)
        if kept:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(kept)
        else:
            os.remove(path)
        for block_id in revoked_auto:
            del verdicts[block_id]
        print(f"撤销抽样自动放行并升级：{len(revoked_auto)} 块")
    if auto_lines:
        path = os.path.join(vdir, "auto_sample.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for i in sorted(auto_lines):
                f.write(auto_lines[i])
        print(f"放行（auto_sample.jsonl）：{len(auto_lines)} 块")
    if escalate:
        print(f"升级子波 {len(escalate)} 个，生成全量任务：")
        xdirs = {
            c: d
            for d in os.listdir(os.path.join(root, "index-X", "EPUB"))
            if (c := jp_align.vol_code(d))
            and os.path.isdir(os.path.join(root, "index-X", "EPUB", d))
        }
        n = next_task_n(triage)
        escalation_tasks = plan_doc.setdefault("escalation_tasks", [])
        for key in escalate:
            ids = [
                i
                for i in (
                    next(g for g in plan_doc["groups"] if tuple(g["key"]) == key)[
                        "sampled"
                    ]
                    + next(g for g in plan_doc["groups"] if tuple(g["key"]) == key)[
                        "deferred"
                    ]
                )
                if i not in verdicts and i not in assigned
            ]
            for k in range(0, len(ids), pr.CHUNK):
                bs = [by_id[i] for i in ids[k : k + pr.CHUNK]]
                if n not in escalation_tasks:
                    escalation_tasks.append(n)
                    save_plan(triage, plan_doc)
                pr.write_task(
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
                n += 1
        print("升级任务已生成，须派子代理深审后重新 --apply")
    if not missing and not escalate:
        print("全部子波样本通过，无升级；抽样审查闭环")
    return 0


def main() -> int:
    parser = triage_parser(__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="生成抽样任务与计划")
    g.add_argument("--apply", action="store_true", help="消费抽样 verdict，升级或放行")
    args = parser.parse_args()
    return plan() if args.plan else apply()


if __name__ == "__main__":
    sys.exit(main())
