"""以当前时间创建并推送 v20260826T1008 格式的 tag，触发 release workflow。

守卫：HEAD 必须已推送（== origin/main），且距上个 tag 有新 commits。
"""

import argparse
import subprocess
import sys
from datetime import datetime


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    git("fetch", "-q", "origin", "main")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        sys.exit("error: HEAD 与 origin/main 不一致（未 push 或远端已更新）")
    try:
        last = git("describe", "--tags", "--abbrev=0")
    except subprocess.CalledProcessError:  # 还没有任何 tag
        last = ""
    if last and git("rev-list", "--count", f"{last}..HEAD") == "0":
        sys.exit(f"error: 距上个 tag {last} 没有新 commits")
    tag = datetime.now().astimezone().strftime("v%Y%m%dT%H%M")
    if tag in git("tag", "-l").splitlines():
        sys.exit(f"error: tag {tag} 已存在（同一分钟内重复运行？）")
    git("tag", "-a", tag, "-m", "")
    git("push", "origin", tag)
    print(tag)


if __name__ == "__main__":
    main()
