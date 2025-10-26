import shutil
from pathlib import Path

import regex as re
from tqdm import tqdm

fixes = (
    ("自于", "自"),
    ("融入在", "融入"),
    ("安心感", "安全感"),
    ("不思议", "不可思议"),
    ("下流胚子", "下流坯子"),
    ("可以其中", "可见其中"),
    ("起到效果", "起到作用"),
    ("体积减少", "体积减小"),
    ("这段期间", "这段时间"),
    ("不净思考", "不经思考"),
    ("一下的话：", "以下的话："),
    ("具(?=(人造)?卫星)", "颗"),
    ("体积大量减少", "体积大幅减小"),
    ("(?<=万幸|意外)的(?=没有)", "地"),
    ("(?<=[打胆冷寒])颤|颤(?=栗)", "战"),
    ("(?<=加醋|准确|确切|坚定|张胆|坦然)的说(?!法)", "地说"),
    (r"(?<=[\d一二三四五六七八九十百千万亿兆])圆(?![顶周])", "元"),
    ("(?<![似目]|什么|新来|怎样)的说(?=[着道些出一]|这种|起来)", "地说"),
    (
        "所(?=占据|影响|追赶|挡住|流出|支配|遮蔽|怨恨|感动|切割|触摸|操纵|散发|包围|监禁|侵)",
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
        for xhtml in (y / vol.name / "OEBPS/Text").glob("*.xhtml"):
            with open(xhtml, "r", encoding="utf-8") as f:
                content = f.read()
            with open(xhtml, "w", encoding="utf-8") as f:
                f.write(fixed(content))


if __name__ == "__main__":
    x2y()
