import re
import shutil
from pathlib import Path

from tqdm import tqdm

fixes = (
    ("自于", "自"),
    ("融入在", "融入"),
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
    ("所(?=占据|影响|追赶|挡住|流出|支配|遮蔽|怨恨|感动|切割)", ""),
)
# ("的说出了", "颗"),
# (?<![目似人众来真—说不可思议怎样到够条])的说(?![！。法辞明话…教不，的])


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
