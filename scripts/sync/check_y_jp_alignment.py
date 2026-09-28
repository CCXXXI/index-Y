"""Y 与 JP 行结构一致性核验（bw_aligned pass 卷）：Y 文件行数 == 对齐单元行数。

不变式：Y 是净化产物，行结构以 JP 为权威——bw_aligned pass 卷的每个正文
单元，Y 侧对应文件的物理行数必须与日文对齐单元一致。规则恢复上游误删内容
时按 JP 行结构行内补入（如原文为同一段落则行内合并，不新增物理行）；
规则删除上游误加内容同理。任何失配都会在此被拦截。

豁免上游登记的文本化图片页（TEXTUAL_IMAGE_HEADERS，如外典扉页图 ↔ 简介页，
两侧本就不同形态）。run_all 在 freshness 门后调用；失配即非零退出。

用法: uv run python scripts/sync/check_y_jp_alignment.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 仓库根（scripts/sync/ 上两级）
sys.path.insert(0, str(ROOT / "scripts" / "sync"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "index-X" / "tools"))

import jp_align

# 运行时经 sys.path 挂 index-X/tools 复用上游公开库；CI 不拉 submodule，静态不可解析
from alignment_rules import (  # ty: ignore[unresolved-import]
    PAIR_RULES,
    TEXTUAL_IMAGE_HEADERS,
)
from lib_triage import repo_root


def main() -> int:
    root = repo_root()
    aligned = jp_align.aligned_vols(Path(root).parent / "index-jp")
    y_root = os.path.join(root, "EPUB")
    bad = []
    checked = 0
    for vol, adir in sorted(aligned.items()):
        yvol = next(
            (
                d
                for d in os.listdir(y_root)
                if d.startswith(f"[{vol}]") and os.path.isdir(os.path.join(y_root, d))
            ),
            None,
        )
        if yvol is None:
            continue
        ytext = os.path.join(y_root, yvol, "OEBPS", "Text")
        for f in sorted(os.listdir(adir)):
            if not f.endswith(".xhtml"):
                continue
            unit = os.path.splitext(f)[0]
            # 豁免：文本化图片页（两侧本不同形态）与上游登记的合法结构差异
            # （PAIR_RULES，如 S5_01_03-06 afterword-moved 行数本就不同）
            if unit.upper() in TEXTUAL_IMAGE_HEADERS or unit.upper() in PAIR_RULES:
                continue
            yf = next(
                (
                    p
                    for p in os.listdir(ytext)
                    if p.startswith(unit) and p.endswith(".xhtml")
                ),
                None,
            )
            if yf is None:
                continue  # 无对应正文单元（包装页等）
            checked += 1
            jp_n = len((adir / f).read_text(encoding="utf-8").splitlines())
            with open(os.path.join(ytext, yf), encoding="utf-8") as fh:
                y_n = len(fh.read().splitlines())
            if jp_n != y_n:
                bad.append(f"{vol}/{yf}: JP {jp_n} 行 vs Y {y_n} 行")
    if bad:
        print(
            "Y 偏离 JP 行结构（行数不一致）。恢复/删除内容须按 JP 行结构"
            "（同段行内合并，不增删物理行）后重跑 x2y："
        )
        print("\n".join(bad[:30]))
        return 1
    print(f"Y 与 JP 行结构一致 ✓（{checked} 个配对单元，{len(aligned)} 卷）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
