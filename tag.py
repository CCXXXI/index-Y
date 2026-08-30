"""以当前时间创建并推送 v20260826T1008 格式的 tag，触发 release workflow。"""

import subprocess
import sys
from datetime import datetime


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> None:
    tag = datetime.now().astimezone().strftime("v%Y%m%dT%H%M")
    if tag in git("tag", "-l").splitlines():
        sys.exit(f"error: tag {tag} 已存在（同一分钟内重复运行？）")
    git("tag", "-a", tag, "-m", "")
    git("push", "origin", tag)
    print(tag)


if __name__ == "__main__":
    main()
