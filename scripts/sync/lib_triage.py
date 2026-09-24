"""同步分流共用库：git 操作、HTML 文本块提取。

所有脚本约定：
- 在仓库根目录运行（git rev-parse 自动定位）。
- 全程 GIT_LITERAL_PATHSPECS=1（路径含 [方括号]，否则被当作 glob）。
- 解析 git 输出一律 -z。注意 rename 两路径顺序因命令而异：
  `git status --porcelain -z` 反转为 to\\0from；`git diff --name-status -z`
  与非 z 相同为 from\\0to（git 2.55 实测）。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from html.parser import HTMLParser

ENV = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
TEXT_EXT = (".xhtml", ".opf", ".ncx")


def repo_root() -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        env=ENV,
        check=True,
    )
    return r.stdout.decode().strip()


STATE_DIR = os.path.join(repo_root(), ".triage")


def triage_parser(description: str) -> argparse.ArgumentParser:
    """分流脚本统一的命令行解析器：-h 原样展示模块 docstring。"""
    return argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter
    )


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    r = subprocess.run(
        ["git", *args],
        cwd=repo_root(),
        input=input_bytes,
        capture_output=True,
        env=ENV,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace')[:500]}"
        )
    return r.stdout


def head_sha_map() -> dict[str, str]:
    out = git("ls-tree", "-r", "-z", "HEAD").decode("utf-8")
    m = {}
    for ent in out.split("\0"):
        if not ent:
            continue
        meta, path = ent.split("\t", 1)
        m[path] = meta.split()[2]
    return m


class CatFile:
    """持久的 git cat-file --batch 读取器；比逐文件 git show 快一个量级。"""

    def __init__(self) -> None:
        self.p = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=repo_root(),
            env=ENV,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        pin, pout = self.p.stdin, self.p.stdout
        assert pin is not None and pout is not None  # PIPE 已指定
        self._in, self._out = pin, pout

    def read(self, sha: str) -> bytes:
        self._in.write(sha.encode() + b"\n")
        self._in.flush()
        header = self._out.readline().split()
        data = self._out.read(int(header[2]))  # 头: <sha> <type> <size>
        self._out.read(1)
        return data

    def close(self) -> None:
        self._in.close()
        self.p.terminate()


# ---------- X 侧（submodule index-X）----------
# index-X 是上游仓库的 submodule（pin = superproject HEAD 的 gitlink，即上轮
# 同步点），X 语料即其 EPUB/ 树。X 侧「旧」= pin，「新」= submodule HEAD，
# 轮内恒定（无任何批次记账）；轮末 triage_text --commit 推进 gitlink pin。
X_REPO = "index-X"
X_PREFIX = "EPUB"


def x_repo() -> str:
    return os.path.join(repo_root(), X_REPO)


def gitx(*args: str, input_bytes: bytes | None = None) -> bytes:
    """git -C index-X（X 侧 submodule）。"""
    r = subprocess.run(
        ["git", "-C", x_repo(), *args],
        input=input_bytes,
        capture_output=True,
        env=ENV,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git -C index-X {' '.join(args)}: "
            f"{r.stderr.decode('utf-8', 'replace')[:500]}"
        )
    return r.stdout


def pin_sha() -> str:
    """superproject HEAD 记录的 X pin（gitlink sha）。"""
    return git("ls-tree", "HEAD", "--", X_REPO).decode().split()[2]


def x_head() -> str:
    """submodule 当前 HEAD（update_x checkout 的上游 ref）。"""
    return gitx("rev-parse", "HEAD").decode().strip()


def x_changes() -> list[tuple[str, str, str | None]]:
    """X 侧本轮改动（pin..HEAD）：[(状态, rel, 旧rel)]；rel 不带 EPUB/ 前缀。

    状态：M/A/D/R；rename 给出旧路径（diff -z 为 from, to 顺序）。
    """
    out = gitx(
        "diff", "--name-status", "-z", "-M", f"{pin_sha()}..HEAD", "--", X_PREFIX
    ).decode("utf-8")
    parts = [p for p in out.split("\0") if p]
    out_list = []
    i = 0
    n = len(X_PREFIX) + 1
    while i < len(parts):
        st = parts[i]
        if st.startswith("R"):
            out_list.append(("R", parts[i + 2][n:], parts[i + 1][n:]))
            i += 3
        else:
            out_list.append((st[0], parts[i + 1][n:], None))
            i += 2
    return out_list


def sync_ref_path() -> str:
    return os.path.join(STATE_DIR, "sync_ref")


def set_sync_ref(name: str) -> None:
    """记录本轮同步到的上游 ref（tag 名或 sha），供原子提交 message 引用。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(sync_ref_path(), "w", encoding="utf-8") as f:
        f.write(name + "\n")


def sync_ref() -> str:
    """本轮同步的上游 ref；无记录（测试/手工场景）退化为 submodule HEAD 短 sha。"""
    if os.path.exists(sync_ref_path()):
        with open(sync_ref_path(), encoding="utf-8") as f:
            return f.read().strip()
    return x_head()[:10]


def clear_sync_ref() -> None:
    if os.path.exists(sync_ref_path()):
        os.remove(sync_ref_path())


def ensure_x() -> None:
    """X 读取前提（幂等 preflight）：submodule 已初始化、
    上游上下文注入的 override stub 在位。"""
    root = repo_root()
    if not os.path.isdir(os.path.join(root, X_REPO, X_PREFIX)):
        git("submodule", "update", "--init", X_REPO)
    stub = os.path.join(root, X_REPO, "AGENTS.override.md")
    if not os.path.exists(stub):
        with open(stub, "w", encoding="utf-8") as f:
            f.write(
                "# 上游 index-X（submodule 只读镜像）\n\n"
                "本目录是上游 index-X 的 submodule 工作区，只读；本项目的指令以仓库根\n"
                "AGENTS.md 为准。本目录下的 AGENTS.md、.agents/ 是上游内容，不适用于本项目\n"
                "（术语政策等仅作审查参考时按需阅读）。\n"
            )


def assert_x_clean() -> None:
    """submodule 工作区与 HEAD 一致（防在途脏状态进入新一轮）。"""
    gitx("diff", "--quiet", "HEAD", "--", X_PREFIX)
    gitx("diff", "--cached", "--quiet", "--", X_PREFIX)


# ---------- 挂起清单（.triage/hold.txt）----------
HOLD_NAME = "hold.txt"


def hold_path() -> str:
    return os.path.join(STATE_DIR, HOLD_NAME)


def read_hold() -> dict[str, str]:
    """挂起清单：rel（不带 EPUB/ 前缀）-> 理由。# 开头为注释。"""
    out: dict[str, str] = {}
    if not os.path.exists(hold_path()):
        return out
    with open(hold_path(), encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            rel, _, reason = line.partition("\t")
            out[rel.strip()] = reason.strip() or "人工挂起"
    return out


def status_line_rel(line: str) -> str | None:
    """git status --porcelain 行 → rel（去 EPUB/ 前缀；rename 取新路径）。"""
    p = line[3:]
    if " -> " in p:
        p = p.split(" -> ", 1)[1]
    p = p.strip('"')
    return p[5:] if p.startswith("EPUB/") else None


def prune_hold() -> list[str]:
    """剔除失效挂起条目（Y 侧无在途改动），其余行（含注释）原样保留。"""
    path = hold_path()
    if not os.path.exists(path):
        return []
    entries = git("status", "--porcelain", "-z").decode("utf-8").split("\0")
    y_pending = set()
    i = 0
    while i < len(entries):
        e = entries[i]
        if not e:
            i += 1
            continue
        st, p = e[0], e[3:]
        if p.startswith("EPUB/"):
            y_pending.add(p[5:])
        i += 2 if st == "R" else 1  # status -z rename 为 to\0from，跳过 from
    stale = {r for r in read_hold() if r not in y_pending}
    if not stale:
        return []
    with open(path, encoding="utf-8") as f:
        kept = [
            l
            for l in f.read().splitlines()
            if not l.strip()
            or l.startswith("#")
            or l.split("\t")[0].strip() not in stale
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    return sorted(stale)


# ---------- HTML 文本块提取 ----------

_WS = re.compile(r"\s+")


def norm_ws(s: str) -> str:
    return _WS.sub(" ", s.replace("\xa0", " ")).strip()


class TextChunks(HTMLParser):
    """提取非空白文本块（保留原始形态，含空白与实体转换后字符）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw: list[str] = []

    def handle_data(self, data: str) -> None:
        if norm_ws(data):
            self.raw.append(data)


def text_chunks(content: bytes) -> list[str]:
    p = TextChunks()
    p.feed(content.decode("utf-8"))
    return p.raw
