import shutil
from pathlib import Path

import regex as re
from tqdm import tqdm

fixes = (
    ("人型", "人形"),
    ("叫作", "叫做"),
    ("安心感", "安全感"),
    ("开学式", "开学典礼"),
    ("(?<=[除并]非)是(?!否)", ""),
    (r"(?<=[\d零一二三四五六七八九十百千万亿兆])圆(?![顶周])", "元"),
    (
        "(?<=(因|由于)[^…。？！]*)(?<!特别|机构|良好|丧命|是这样|冲击波|立场上|「暗部」)(之故|所致|的关系|的缘故)",
        "",
    ),
)


def fixed(content: str) -> str:
    for old, new in fixes:
        content = re.sub(old, new, content)
    return content


def x2y():
    x, y = Path("X"), Path("Y")
    shutil.rmtree(y)
    for vol in tqdm(list(x.iterdir())):
        shutil.copytree(vol, y / vol.name)
        for file in (y / vol.name).rglob("*"):
            if file.suffix not in (".xhtml", ".opf", ".ncx"):
                continue
            with open(file, "r", encoding="utf-8") as f:
                content = f.read()
            with open(file, "w", encoding="utf-8") as f:
                f.write(fixed(content))


if __name__ == "__main__":
    x2y()
