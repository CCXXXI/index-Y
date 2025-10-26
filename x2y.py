import shutil
from pathlib import Path

import regex as re
from tqdm import tqdm

fixes = (
    ("自于", "自"),
    ("人型", "人形"),
    ("吵杂", "嘈杂"),
    ("砂尘", "沙尘"),
    ("护镜", "护目镜"),
    ("融入在", "融入"),
    ("再重申", "重申"),
    ("安心感", "安全感"),
    ("已经快要", "快要"),
    ("不思议", "不可思议"),
    ("三叉路口", "三岔路口"),
    ("下流胚子", "下流坯子"),
    ("可以其中", "可见其中"),
    ("溶在一起", "融在一起"),
    ("起到效果", "起到作用"),
    ("体积减少", "体积减小"),
    ("干下的罪", "犯下的罪"),
    ("直接了当", "直截了当"),
    ("这段期间", "这段时间"),
    ("不净思考", "不经思考"),
    ("并非是(?!否)", "并非"),
    ("(?<=用尽了?)全部", ""),
    ("(?<!对)地来说", "地说"),
    ("具(?=(人造)?卫星)", "颗"),
    ("一下的话：", "以下的话："),
    ("体积大量减少", "体积大幅减小"),
    ("超越这个记录", "超越这个纪录"),
    ("(?<=万幸|意外)的(?=没有)", "地"),
    ("(?<=[打胆冷寒])颤|颤(?=栗)", "战"),
    ("(?<=一点一滴|一声不响)的消失", "地消失"),
    ("只(?=(破|新|零圆|型号略旧的)手机)", "部"),
    ("(?<=加醋|准确|确切|坚定|张胆|坦然)的说(?!法)", "地说"),
    (r"(?<=[\d一二三四五六七八九十百千万亿兆])圆(?![顶周])", "元"),
    ("(?<![似目]|什么|新来|怎样)的说(?=[着道些出一]|这种|起来)", "地说"),
    ("支(?=(智能|廉价|粉红色智能型|掉在小商店街的|有GPS全球定位系统的)?手机)", "部"),
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
