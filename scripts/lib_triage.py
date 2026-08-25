"""同步分流共用库：git 操作、HTML 解析、文本块对齐。

所有脚本约定：
- 在仓库根目录运行（git rev-parse 自动定位）。
- 全程 GIT_LITERAL_PATHSPECS=1（路径含 [方括号]，否则被当作 glob）。
- 解析 git 输出一律 -z。注意 -z 模式 rename 输出顺序为 to\\0from（与非 z 相反）。
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
from collections import Counter
from html.parser import HTMLParser

ENV = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp")
TEXT_EXT = (".xhtml", ".opf", ".ncx")


def repo_root() -> str:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, env=ENV, check=True)
    return r.stdout.decode().strip()


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    r = subprocess.run(["git", *args], cwd=repo_root(), input=input_bytes,
                       capture_output=True, env=ENV)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: "
                           f"{r.stderr.decode('utf-8', 'replace')[:500]}")
    return r.stdout


def status_entries() -> list[tuple[str, str, str | None]]:
    """暂存区+工作区状态。返回 (XY, path, rename_from_or_None)。"""
    out = []
    for e in git("status", "--porcelain", "-z").decode("utf-8").split("\0"):
        if not e:
            continue
        out.append((e[:2], e[3:], None))
    # rename 在 -z 下占两个条目槽位：重新按 diff --cached 解析 staged rename
    return out


def staged_renames() -> list[tuple[str, str]]:
    """暂存的 rename 对，返回 (from, to)。"""
    out = git("diff", "--cached", "--name-status", "-z", "-M",
              "--diff-filter=R").decode("utf-8")
    parts = [p for p in out.split("\0") if p]
    pairs = []
    i = 0
    while i < len(parts):
        assert parts[i].startswith("R"), parts[i]
        # -z 模式顺序为 to, from（与非 z 相反）
        pairs.append((parts[i + 2], parts[i + 1]))
        i += 3
    return pairs


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
        self.p = subprocess.Popen(["git", "cat-file", "--batch"],
                                  cwd=repo_root(), env=ENV,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, sha: str) -> bytes:
        self.p.stdin.write(sha.encode() + b"\n")
        self.p.stdin.flush()
        header = self.p.stdout.readline().split()
        data = self.p.stdout.read(int(header[2]))  # 头: <sha> <type> <size>
        self.p.stdout.read(1)
        return data

    def close(self) -> None:
        self.p.stdin.close()
        self.p.terminate()


def stage_content(path: str, content: bytes) -> None:
    """把内存中的内容写入索引（工作区不动），剩余改动自动留作未提交。"""
    blob = git("hash-object", "-w", "--stdin", input_bytes=content).strip().decode()
    mode = git("ls-tree", "HEAD", "--", path).split()[0].decode()
    git("update-index", "--cacheinfo", f"{mode},{blob},{path}")


def commit_paths(subject: str, body: str, paths: list[str]) -> None:
    """只提交指定路径（工作区状态），其余暂存项不受影响。"""
    args = ["commit", "-q", "-m", subject]
    if body:
        args += ["-m", body]
    git(*args, "--", *paths)


def staged_path_count() -> int:
    return len([p for p in git("diff", "--cached", "--name-only", "-z")
                .decode("utf-8").split("\0") if p])


# ---------- HTML 解析 ----------

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


class Events(HTMLParser):
    """事件序列 + 属性多重集合，用于纯格式化判定。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.seq: list[tuple] = []
        self.attrs: Counter = Counter()

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = dict(attrs)
        for k, v in attrs:
            self.attrs[(tag, k, v)] += 1
        if tag == "img":
            self.seq.append(("img", ad.get("src", "")))
        elif tag == "a":
            self.seq.append(("a", ad.get("href", "")))
        elif tag == "br":
            self.seq.append(("br",))
        elif tag == "p" and not attrs:
            self.seq.append(("p",))
        else:
            self.seq.append(("tag", tag))

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_data(self, data: str) -> None:
        t = norm_ws(data)
        if t:
            self.seq.append(("text", t))


def parse_events(content: bytes) -> tuple[list[tuple], list]:
    p = Events()
    p.feed(content.decode("utf-8"))
    return p.seq, sorted(p.attrs.items())


def is_pure_formatting(old: bytes, new: bytes) -> bool:
    """文本一致 + 属性多重集合一致 + 事件序列一致（仅允许无属性 <p> 包裹 img 的增减）。"""
    os_, oa = parse_events(old)
    ns, na = parse_events(new)
    if oa != na:
        return False

    def filt(s):
        return [e for i, e in enumerate(s)
                if not (e == ("p",) and i + 1 < len(s) and s[i + 1][0] == "img")]

    return filt(os_) == filt(ns)


# ---------- 文本改动提取（块级 + 片段级） ----------

def chunk_changes(old: bytes, new: bytes) -> list[tuple[str, str]] | None:
    """对齐两侧文本块，返回 1:1 replace 的 (旧, 新) 列表；有增删则返回 None。"""
    oc = [norm_ws(c) for c in text_chunks(old)]
    nc = [norm_ws(c) for c in text_chunks(new)]
    sm = difflib.SequenceMatcher(a=oc, b=nc, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            return None
        out.extend((oc[i], nc[j]) for i, j in zip(range(i1, i2), range(j1, j2)))
    return out


def frag_set(changes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """块内最小差异片段集合。块级比较会因旧 Y 已被规则改写而误判，片段级不会。"""
    frags = []
    for o, n in changes:
        sm = difflib.SequenceMatcher(a=o, b=n, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                frags.append((o[i1:i2], n[j1:j2]))
    return sorted(frags)


def raw_variants(chunk: str) -> list[str]:
    """文本块在文件中的可能原始形态（实体编码变体）。"""
    return [chunk,
            chunk.replace("&", "&amp;"),
            chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")]


def revert_text_chunks(old_text: str, new_text: str) -> str | None:
    """返回「新结构 + 旧文本」的中间版本；无法安全还原（文本增删/定位歧义）返回 None。

    对照 docs/sync-triage.md 第 5 节：逐块对齐后把新块替换回旧块，
    要求在文件中唯一定位。调用方需自行验证返回值与 old_text 文本块一致。
    """
    oc, nc = text_chunks(old_text.encode()), text_chunks(new_text.encode())
    on, nn = [norm_ws(c) for c in oc], [norm_ws(c) for c in nc]
    if on == nn:
        return new_text
    sm = difflib.SequenceMatcher(a=on, b=nn, autojunk=False)
    staged = new_text
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            return None
        for oi, nj in zip(range(i1, i2), range(j1, j2)):
            nvs, ovs = raw_variants(nc[nj]), raw_variants(oc[oi])
            hit = next((k for k, v in enumerate(nvs) if staged.count(v) == 1), None)
            if hit is None:
                return None
            staged = staged.replace(nvs[hit], ovs[hit], 1)
    if [norm_ws(c) for c in text_chunks(staged.encode())] != on:
        return None
    return staged
