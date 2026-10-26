"""Build release EPUBs with the OCF-required mimetype entry layout."""

import argparse
import zipfile
from pathlib import Path


def package_epub(source: Path, destination: Path) -> None:
    """Package one volume, writing an uncompressed mimetype entry first."""
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(
            "mimetype",
            (source / "mimetype").read_bytes(),
            compress_type=zipfile.ZIP_STORED,
        )
        for path in sorted(source.rglob("*")):
            if path.is_file() and path != source / "mimetype":
                archive.write(
                    path,
                    path.relative_to(source).as_posix(),
                    compress_type=zipfile.ZIP_DEFLATED,
                )


def package_release(epub_root: Path, build: Path, tag: str) -> None:
    """Package every volume and collect the EPUBs into the release archive."""
    build.mkdir(parents=True, exist_ok=True)
    epubs = []
    for volume in sorted(epub_root.iterdir()):
        if volume.is_dir():
            epub = build / f"{volume.name}.epub"
            package_epub(volume, epub)
            epubs.append(epub)
    with zipfile.ZipFile(build / f"index-Y-{tag}.zip", "w") as archive:
        for epub in epubs:
            archive.write(epub, epub.name, compress_type=zipfile.ZIP_DEFLATED)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub_root", type=Path)
    parser.add_argument("build", type=Path)
    parser.add_argument("tag")
    args = parser.parse_args()
    package_release(args.epub_root, args.build, args.tag)


if __name__ == "__main__":
    main()
