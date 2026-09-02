"""同步上游 X 版的完整流程：

1. 解压 zip，找到其中所有 epub（支持完整下载或部分挑选两种情况）；
2. 每个 epub 解压后整体替换 X/ 下的同名文件夹——旧版先整个删除，
   防止上游文件改名或删除后残留旧文件；
3. 全部替换完成后重跑 x2y.py 重新生成 Y/。
"""

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from tqdm import tqdm
from x2y import x2y

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zip", type=Path, help="上游下载的 zip 路径")
    args = parser.parse_args()
    zip_path: Path = args.zip
    x_dir = REPO_ROOT / "X"

    with zipfile.ZipFile(zip_path) as outer:
        epubs = [n for n in outer.namelist() if n.lower().endswith(".epub")]
        if not epubs:
            sys.exit(f"错误：{zip_path} 中没有 epub 文件")

        for name in tqdm(epubs, "解压"):
            vol = Path(name).stem
            target = x_dir / vol
            # 先解压到仓库内的临时目录（.gitignore 排除）并校验，确认无误后
            # 再替换；与 X/ 同卷可直接 move，且避免解压失败时旧版已被删除
            with tempfile.TemporaryDirectory(dir=REPO_ROOT,
                                             prefix=".update-x-") as tmp:
                staging = Path(tmp) / "extract"
                with outer.open(name) as f, zipfile.ZipFile(f) as epub:
                    epub.extractall(staging)
                if not (staging / "mimetype").is_file():
                    sys.exit(f"错误：{name} 不是有效的 epub（缺少 mimetype），已中止")
                existed = target.exists()
                if existed:
                    shutil.rmtree(target)
                shutil.move(str(staging), target)

    x2y()


if __name__ == "__main__":
    main()
