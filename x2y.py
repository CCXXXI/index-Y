import re
import shutil
from pathlib import Path

from tqdm import tqdm

fixes = (("所(?=占据)", ""),)


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
