"""verdict JSONL 的严格读取与共享校验。"""

from __future__ import annotations

import json
import os
import re

import jp_align

VERDICTS = {"ok", "suspect", "unsure", "unlocated"}
CLASSES = {"form", "semantic", "auto"}
REQUIRED_FIELDS = ("id", "verdict", "cls", "jp", "reason")


class VerdictError(ValueError):
    """审查产出格式或内容不合法。"""


def norm_jp(s: str) -> str:
    """归并空白并剥页码前缀〔…〕，供 jp 引用逐字子串比对。"""
    return re.sub(r"〔[^〕]*〕|\s+", "", s)


def validate_evidence(
    triage: str, blocks: list[dict], verdicts: dict[int, dict]
) -> None:
    """非豁免语义判定必须有真实原文；引文片段可在块的任一涉及卷中命中。"""
    by_id = {b.get("id", i): b for i, b in enumerate(blocks)}
    jp_cache: dict[str, list[str]] = {}
    errors: list[str] = []

    def sources(vol: str) -> list[str]:
        if vol not in jp_cache:
            texts = []
            for suffix in (".txt", ".aligned.txt"):
                path = os.path.join(triage, "jp_text", f"{vol}{suffix}")
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        texts.append(norm_jp(f.read()))
            jp_cache[vol] = texts
        return jp_cache[vol]

    for block_id, value in sorted(verdicts.items()):
        block = by_id[block_id]
        jp = value["jp"]
        required = (
            value["cls"] == "semantic"
            and value["verdict"] != "unlocated"
            and not block.get("jp_exempt", False)
        )
        if not jp.strip():
            if required:
                errors.append(f"id {block_id}: 非豁免 semantic verdict 的 jp 不能为空")
            continue
        parts = []
        for fragment in re.split(r"…|\.{3,}", jp):
            normalized = norm_jp(fragment)
            if normalized:
                parts.append(normalized)
        if not parts:
            errors.append(f"id {block_id}: jp 引用必须含非省略号的非空片段")
            continue
        vols = []
        for path in block["files"]:
            vol = jp_align.vol_code(path)
            if vol is None:
                errors.append(f"id {block_id}: 无法从路径识别卷码: {path}")
                continue
            if vol not in vols:
                vols.append(vol)
        available = [(vol, text) for vol in vols for text in sources(vol)]
        if not available:
            errors.append(f"id {block_id}: 涉及卷 {vols} 无 jp_text 而 jp 引用非空")
            continue
        for part in parts:
            if not any(part in text for _vol, text in available):
                errors.append(
                    f"id {block_id}: jp 引用不在涉及卷 {vols} 原文中"
                    f"（疑似编造）: {part[:60]}"
                )
    if errors:
        raise VerdictError("\n".join(errors))


def load_verdicts(triage: str, blocks: list[dict]) -> dict[int, dict]:
    """读取全部 verdict；任一记录非法则拒绝整批，不返回部分结果。"""
    domain = {b.get("id", i) for i, b in enumerate(blocks)}
    verdicts: dict[int, dict] = {}
    errors: list[str] = []
    vdir = os.path.join(triage, "verdicts")
    if not os.path.isdir(vdir):
        return verdicts
    for fname in sorted(f for f in os.listdir(vdir) if f.endswith(".jsonl")):
        with open(os.path.join(vdir, fname), encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                if not line.strip():
                    continue
                where = f"{fname}:{ln}"
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{where}: JSON 不可解析（{exc.msg}）")
                    continue
                if not isinstance(value, dict):
                    errors.append(f"{where}: verdict 必须是 JSON 对象")
                    continue
                missing = [key for key in REQUIRED_FIELDS if key not in value]
                if missing:
                    errors.append(f"{where}: 缺字段 {', '.join(missing)}")
                    continue
                block_id = value["id"]
                if isinstance(block_id, bool) or not isinstance(block_id, int):
                    errors.append(f"{where}: id 必须是整数")
                    continue
                if block_id not in domain:
                    errors.append(f"{where}: 未知 id {block_id}")
                    continue
                if block_id in verdicts:
                    errors.append(f"{where}: 重复 id {block_id}")
                    continue
                kind = value["verdict"]
                if not isinstance(kind, str) or kind not in VERDICTS:
                    errors.append(f"{where}: 非法 verdict {kind!r}")
                    continue
                cls = value["cls"]
                if not isinstance(cls, str) or cls not in CLASSES:
                    errors.append(f"{where}: 非法 cls {cls!r}")
                    continue
                for key in ("jp", "reason"):
                    if not isinstance(value[key], str):
                        errors.append(f"{where}: {key} 必须是字符串")
                        break
                else:
                    verdicts[block_id] = value
    if errors:
        raise VerdictError("\n".join(errors))
    validate_evidence(triage, blocks, verdicts)
    return verdicts
