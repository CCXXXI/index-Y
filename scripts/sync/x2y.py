"""X → Y 转换引擎。规则数据在仓库根目录 rules/ 下，代码与数据分离。

规则文件：rules/_common.tsv 为全卷通用段；rules/<卷名>.tsv 为分卷段
（文件名 = X/ 下的卷目录名，无规则的卷不建文件）。
格式：首行固定表头 `旧<TAB>新<TAB>注释`，规则行每行 `旧<TAB>新<TAB>注释`
（旧/新为 regex 源文本；注释为纯文本，可空——此时行尾是 tab，须防编辑器
吞掉，见 .editorconfig；删除型规则 new 为空）。# 开头为非规则行（禁用规则、
卷级说明），同样须 3 字段；空行禁止。全文件列数一致是 GitHub、PyCharm 等
严格 TSV 预览正常渲染的前提。应用顺序：分卷规则先于通用规则，文件内自上而下。
"""

import argparse
import shutil
import sys
from pathlib import Path

import regex as re
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_triage import ensure_x

ROOT = Path(__file__).parent.parent.parent
RULES_DIR = ROOT / "rules"
TEXT_EXT = (".xhtml", ".opf", ".ncx")
HEADER = "旧\t新\t注释"

ensure_x()  # preflight：load_rule_records 列 X/ 卷目录，联接须先存在


def load_rule_records() -> list[dict]:
    """加载 rules/*.tsv 为规则记录（含出处行号）并校验；异常直接报错退出。"""
    x_vols = {p.name for p in (ROOT / "X").iterdir() if p.is_dir()}
    records = []
    errors = []
    for tsv in sorted(RULES_DIR.glob("*.tsv")):
        section = "*" if tsv.stem == "_common" else tsv.stem
        if section != "*" and section not in x_vols:
            errors.append(f"{tsv.name}: 文件名与 X/ 下卷目录不对应")
        seen = {}
        lines = tsv.read_text(encoding="utf-8-sig").splitlines()
        if not lines or lines[0] != HEADER:
            errors.append(f"{tsv.name}: 首行须为表头「{HEADER}」")
            continue
        for lineno, raw in enumerate(lines[1:], 2):
            fields = raw.split("\t")
            if len(fields) != 3:
                errors.append(
                    f"{tsv.name}:{lineno}: 字段数 {len(fields)} ≠ 3"
                    "（空字段行尾的 tab 可能被编辑器吞掉；空行禁止）"
                )
                continue
            old, new, note = fields
            if old.startswith("#"):
                continue
            if not old:
                errors.append(f"{tsv.name}:{lineno}: old 为空（说明行请以 # 开头）")
                continue
            if old in seen:
                errors.append(f"{tsv.name}:{lineno}: old 与第 {seen[old]} 行重复")
            seen[old] = lineno
            try:
                re.compile(old)
            except re.error as e:
                errors.append(f"{tsv.name}:{lineno}: 正则编译失败: {e}")
            records.append(
                {
                    "section": section,
                    "lineno": lineno,
                    "old": old,
                    "new": new,
                    "note": note,
                }
            )
    if errors:
        sys.exit("rules/ 校验失败：\n" + "\n".join(errors))
    return records


def load_rules() -> dict[str, list[tuple[str, str]]]:
    """把规则记录按段聚合为应用管道。"""
    fixes: dict[str, list[tuple[str, str]]] = {}
    for r in load_rule_records():
        fixes.setdefault(r["section"], []).append((r["old"], r["new"]))
    return fixes


fixes = load_rules()


def fixed(vol: str, content: str) -> str:
    for old, new in fixes.get(vol, []) + fixes["*"]:
        content = re.sub(old, new, content)
    return content


def x2y():
    ensure_x()  # submodule init + X/ 联接 + override stub（幂等）
    x, y = ROOT / "X", ROOT / "Y"
    if y.exists():  # 只豁免「不存在」；占用/只读等删除错误保持响亮失败
        shutil.rmtree(y)
    for vol in tqdm(list(x.iterdir()), "x2y"):
        shutil.copytree(vol, y / vol.name)
        for file in (y / vol.name).rglob("*"):
            if file.suffix not in TEXT_EXT:
                continue
            with open(file, "r", encoding="utf-8") as f:
                content = f.read()
            with open(file, "w", encoding="utf-8", newline="") as f:
                f.write(fixed(vol.name, content))


if __name__ == "__main__":
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    x2y()
