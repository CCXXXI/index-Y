import shutil
from pathlib import Path

import regex as re
from tqdm import tqdm

fixes: dict[str, list[tuple[str, str]]] = {
    "*": [
        ("塑胶", "塑料"),
        ("人型", "人形"),
        ("叫作", "叫做"),
        ("安心感", "安全感"),
        ("几乎已经", "几乎"),
        ("(?<=[除并]非)是(?!否)", ""),
        (r"(?<=[\d零一二三四五六七八九十百千万亿兆])圆(?![顶周])", "元"),
        (
            "(?<=(因|由于)[^…。？！]*)(?<!特别|机构|良好|丧命|是这样|冲击波|立场上|「暗部」)(之故|所致|的关系|的缘故)",
            "",
        ),
    ],
    "[S0_00]读前必看": [
        ("以台版为本体", "以台版为基础"),
        ("以本人一己之力", "以一己之力"),
        ("没必要感觉", "感觉没必要"),
        ("部分模糊，部分没有翻译", "部分图片模糊，部分文字没有翻译"),
        ("部分模糊，繁体字看着累", "部分图片模糊，繁体字阅读困难"),
        ("原图重新修嵌，美观而且清晰", "原图重新修复嵌字，美观且清晰"),
        ("部分用词不符合大陆人习惯", "部分用词不符合大陆习惯"),
        ("大部分跟随维基或汉化组的最新标准", "大部分遵循维基或汉化组的最新标准"),
        ("持续更新，且有问题可以实时反馈", "持续更新，且可以实时反馈问题"),
        (
            "自己主观上觉得有问题的，希望能自己结合日语原版、英翻，来给出明确的解决方案。",
            "如果觉得有问题，希望能结合日语原版和英译，给出明确的解决方案。",
        ),
    ],
    "[S1_06]某魔法的禁书目录 06X": [
        ("开学式", "开学典礼"),
    ],
}


def fixed(vol: str, content: str) -> str:
    for old, new in fixes["*"] + (fixes[vol] if vol in fixes else []):
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
                f.write(fixed(vol.name, content))


if __name__ == "__main__":
    x2y()
