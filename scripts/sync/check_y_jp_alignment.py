"""Y 与 JP 行结构一致性核验（bw_aligned pass 卷）：Y 文件行数 == 对齐单元行数。

不变式：Y 是净化产物，行结构以 JP 为权威——bw_aligned pass 卷的每个正文
单元，Y 侧对应文件的物理行数必须与日文对齐单元一致。规则恢复上游误删内容
时按 JP 行结构行内补入（如原文为同一段落则行内合并，不新增物理行）；
规则删除上游误加内容同理。任何失配都会在此被拦截。

豁免上游登记的文本化图片页（TEXTUAL_IMAGE_HEADERS）与结构差异（PAIR_RULES），
以及本脚本逐卷核实、显式登记的合法缺席单元（UNPAIRED_NUMERIC_UNITS）。
run_all 在 freshness 门后调用；失配即非零退出。

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

# 数字正文单元缺 Y 对应文件原则上失败；仅放行逐卷核实过的完整单元码。
# 新增条目须说明结构依据，不得根据正文内容猜测配对或豁免。
UNPAIRED_NUMERIC_UNITS = frozenset(
    {
        # BW 源卷末的作者/插画师介绍，Y 未收录对应独立单元。
        "S1_01-08",
        "S1_02-10",
        "S1_03-08",
        "S1_04-08",
        "S1_05-08",
        "S1_06-10",
        "S1_07-10",
        "S1_08-12",
        "S1_09-10",
        "S1_10-10",
        "S1_11-13",
        "S1_12-13",
        "S1_13-11",
        "S1_14-08",
        "S1_15-11",
        "S1_16-13",
        "S1_17-12",
        "S1_18-24",
        "S1_19-09",
        "S1_20-10",
        "S1_21-10",
        "S1_22-12",
        "S1_23-10",
        "S1_24-12",
        "S2_01-13",
        # Y 文件为 S2_11-Afterwords.xhtml，无数字内容序可供机械配对。
        "S2_11-11",
        # BW 源卷末图片页，Y 无同编号独立单元。
        "S5_03_02-09",
        "S6_24.06.07-13",
    }
)


def main() -> int:
    root = repo_root()
    aligned = jp_align.aligned_vols(Path(root).parent / "index-jp")
    y_root = os.path.join(root, "EPUB")
    bad = []
    checked = 0
    exempted = 0
    for vol, adir in sorted(aligned.items()):
        vol_matches = [
            d
            for d in os.listdir(y_root)
            if jp_align.vol_code(d) == vol and os.path.isdir(os.path.join(y_root, d))
        ]
        if not vol_matches:
            bad.append(f"{vol}: JP pass 卷在 Y 中缺失")
            continue
        if len(vol_matches) != 1:
            bad.append(f"{vol}: Y 卷目录配对不唯一：{', '.join(vol_matches)}")
            continue
        yvol = vol_matches[0]
        ytext = os.path.join(y_root, yvol, "OEBPS", "Text")
        if not os.path.isdir(ytext):
            bad.append(f"{vol}/{yvol}: Y 缺少 OEBPS/Text 正文目录")
            continue
        y_files = [name for name in os.listdir(ytext) if name.endswith(".xhtml")]
        vol_checked = 0
        vol_units = 0
        vol_failed = False
        for f in sorted(os.listdir(adir)):
            if not f.endswith(".xhtml"):
                continue
            unit = os.path.splitext(f)[0]
            # 豁免：文本化图片页（两侧本不同形态）与上游登记的合法结构差异
            # （PAIR_RULES，如 S5_01_03-06 afterword-moved 行数本就不同）
            if unit.upper() in TEXTUAL_IMAGE_HEADERS or unit.upper() in PAIR_RULES:
                exempted += 1
                continue
            vol_units += 1
            # 内容序后只可接 end 或下划线后缀；既避免 1 前缀误配 10，
            # 也允许无对应 Y 文件的非数字单元（如 fm01）诚实跳过。
            exact = f"{unit}.xhtml".upper()
            prefixed = f"{unit}_".upper()
            candidates = [
                name
                for name in y_files
                if name.upper() == exact or name.upper().startswith(prefixed)
            ]
            if not candidates:
                if jp_align.unit_code(f) is None:
                    continue  # 非数字包装单元（如 fm01）无对应 Y 文件时合法缺席
                if unit.upper() in UNPAIRED_NUMERIC_UNITS:
                    exempted += 1
                    continue
                bad.append(f"{vol}/{unit}: 数字正文单元在 Y 中缺失")
                vol_failed = True
                continue
            if len(candidates) != 1:
                bad.append(f"{vol}/{unit}: Y 单元配对不唯一：{', '.join(candidates)}")
                vol_failed = True
                continue
            yf = candidates[0]
            checked += 1
            vol_checked += 1
            jp_n = len((adir / f).read_text(encoding="utf-8").splitlines())
            with open(os.path.join(ytext, yf), encoding="utf-8") as fh:
                y_n = len(fh.read().splitlines())
            if jp_n != y_n:
                bad.append(f"{vol}/{yf}: JP {jp_n} 行 vs Y {y_n} 行")
        if vol_units and not vol_checked and not vol_failed:
            bad.append(f"{vol}/{yvol}: JP pass 卷有正文单元，但 Y 配对数为 0")
    if bad:
        print("Y/JP 配对或行结构核验失败：")
        print("\n".join(bad[:30]))
        return 1
    print(
        f"Y 与 JP 行结构一致 ✓（{checked} 个配对单元，{len(aligned)} 卷；"
        f"{exempted} 个合法豁免）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
