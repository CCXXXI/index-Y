import subprocess
from pathlib import Path

cmd = ["git", "diff", "--no-index", "--ignore-cr-at-eol", "--shortstat"]


def stat():
    for vol in Path("X").iterdir():
        print(vol.name, end=":")
        subprocess.run(cmd + [f"X/{vol.name}", f"Y/{vol.name}"])
    print("total", end=":")
    subprocess.run(cmd + ["X", "Y"])


if __name__ == "__main__":
    stat()
