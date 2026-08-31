"""X → Y 转换引擎。规则数据在仓库根目录 rules/ 下，代码与数据分离。

规则文件：rules/_common.tsv 为全卷通用段；rules/<卷名>.tsv 为分卷段
（文件名 = X/ 下的卷目录名，无规则的卷不建文件）。
格式：每行 `旧<TAB>新`（均为 regex 源文本，删除型规则 new 为空、行尾是 tab），
# 开头为注释，空行忽略。应用顺序：分卷规则先于通用规则，文件内自上而下。
"""
import shutil
import sys
from pathlib import Path

import regex as re
from tqdm import tqdm

ROOT = Path(__file__).parent.parent
RULES_DIR = ROOT / "rules"
TEXT_EXT = (".xhtml", ".opf", ".ncx")


def load_rules() -> dict[str, list[tuple[str, str]]]:
    """加载 rules/*.tsv 并校验；任何异常直接报错退出（fail-loud）。"""
    x_vols = {p.name for p in (ROOT / "X").iterdir() if p.is_dir()}
    fixes: dict[str, list[tuple[str, str]]] = {}
    errors = []
    for tsv in sorted(RULES_DIR.glob("*.tsv")):
        section = "*" if tsv.stem == "_common" else tsv.stem
        if section != "*" and section not in x_vols:
            errors.append(f"{tsv.name}: 文件名与 X/ 下卷目录不对应")
        seen = {}
        for lineno, raw in enumerate(
                tsv.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not raw or raw.startswith("#"):
                continue
            fields = raw.split("\t")
            if len(fields) != 2:
                errors.append(
                    f"{tsv.name}:{lineno}: 字段数 {len(fields)} ≠ 2"
                    "（删除型规则行尾的 tab 可能被编辑器吞掉）")
                continue
            old, new = fields
            if old in seen:
                errors.append(
                    f"{tsv.name}:{lineno}: old 与第 {seen[old]} 行重复")
            seen[old] = lineno
            try:
                re.compile(old)
            except re.error as e:
                errors.append(f"{tsv.name}:{lineno}: 正则编译失败: {e}")
            fixes.setdefault(section, []).append((old, new))
    if errors:
        sys.exit("rules/ 校验失败：\n" + "\n".join(errors))
    return fixes


fixes = load_rules()


def fixed(vol: str, content: str) -> str:
    for old, new in fixes.get(vol, []) + fixes["*"]:
        content = re.sub(old, new, content)
    return content


def x2y():
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
    x2y()
