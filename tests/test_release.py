import struct
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import package_release


def make_volume(root: Path) -> Path:
    volume = root / "【S1_01】测试卷"
    (volume / "META-INF").mkdir(parents=True)
    (volume / "OEBPS" / "Text").mkdir(parents=True)
    (volume / "mimetype").write_bytes(b"application/epub+zip")
    (volume / "META-INF" / "container.xml").write_text("<container/>", encoding="utf-8")
    (volume / "OEBPS" / "Text" / "chapter.xhtml").write_text(
        "<p>正文</p>", encoding="utf-8"
    )
    return volume


def test_package_epub_writes_required_mimetype_layout(tmp_path: Path) -> None:
    volume = make_volume(tmp_path)
    destination = tmp_path / "book.epub"

    package_release.package_epub(volume, destination)

    with zipfile.ZipFile(destination) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert first.header_offset == 0
        assert archive.read("mimetype") == b"application/epub+zip"
        assert archive.read("OEBPS/Text/chapter.xhtml") == "<p>正文</p>".encode()

    data = destination.read_bytes()
    assert data[:4] == b"PK\x03\x04"
    compression = struct.unpack_from("<H", data, 8)[0]
    name_length, extra_length = struct.unpack_from("<HH", data, 26)
    assert compression == zipfile.ZIP_STORED
    assert data[30 : 30 + name_length] == b"mimetype"
    assert extra_length == 0


def test_package_epub_preserves_nested_mimetype_resources(tmp_path: Path) -> None:
    volume = make_volume(tmp_path)
    (volume / "OEBPS" / "mimetype").write_bytes(b"nested resource")
    destination = tmp_path / "book.epub"

    package_release.package_epub(volume, destination)

    with zipfile.ZipFile(destination) as archive:
        assert "OEBPS/mimetype" in archive.namelist()
        assert archive.read("OEBPS/mimetype") == b"nested resource"
        assert archive.namelist().count("mimetype") == 1


def test_package_release_preserves_aggregate_zip(tmp_path: Path) -> None:
    epub_root = tmp_path / "EPUB"
    volume = make_volume(epub_root)
    build = tmp_path / "build"

    package_release.package_release(epub_root, build, "v-test")

    epub = build / f"{volume.name}.epub"
    aggregate = build / "index-Y-v-test.zip"
    assert epub.is_file()
    with zipfile.ZipFile(aggregate) as archive:
        assert archive.namelist() == [epub.name]
        assert archive.read(epub.name) == epub.read_bytes()


def test_cli_builds_release(tmp_path: Path) -> None:
    epub_root = tmp_path / "EPUB"
    volume = make_volume(epub_root)
    build = tmp_path / "build"
    script = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"

    subprocess.run(
        [sys.executable, str(script), str(epub_root), str(build), "v-test"],
        check=True,
        cwd=tmp_path,
    )

    assert (build / f"{volume.name}.epub").is_file()
    assert (build / "index-Y-v-test.zip").is_file()
