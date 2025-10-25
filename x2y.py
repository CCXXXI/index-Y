import shutil
from pathlib import Path

from tqdm import tqdm


def x2y():
    x, y = Path("X"), Path("Y")
    shutil.rmtree(y)
    for vol in tqdm(list(x.iterdir())):
        shutil.copytree(vol, y / vol.name)


if __name__ == "__main__":
    x2y()
