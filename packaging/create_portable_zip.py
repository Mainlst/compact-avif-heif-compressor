"""Package the complete portable Windows folder with standard-library tools."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile
import zipfile


PROJECT = Path(__file__).resolve().parent.parent


def create_portable_zip(source: Path, destination: Path) -> Path:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_dir() or not (source / "ImageCompressor.exe").is_file():
        raise ValueError(f"Portable build is missing: {source / 'ImageCompressor.exe'}")
    if destination == source or source in destination.parents:
        raise ValueError("The ZIP must be outside the portable build folder.")
    entries = sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix())
    if any(path.is_symlink() for path in entries):
        raise ValueError("The portable build must contain ordinary files and directories.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}-", suffix=".zip.tmp", dir=destination.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in entries:
                name = path.relative_to(source.parent).as_posix()
                directory = path.is_dir()
                info = zipfile.ZipInfo(name + ("/" if directory else ""), (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = ((0o40755 if directory else 0o100644) << 16) | (0x10 if directory else 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                if directory:
                    archive.writestr(info, b"")
                else:
                    info.file_size = path.stat().st_size
                    with path.open("rb") as original, archive.open(info, "w", force_zip64=True) as packed:
                        shutil.copyfileobj(original, packed, length=1024 * 1024)
        with zipfile.ZipFile(temporary) as archive:
            damaged = archive.testzip()
            if damaged:
                raise ValueError(f"ZIP integrity check failed: {damaged}")
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PROJECT / "dist" / "ImageCompressor")
    parser.add_argument("--output", type=Path, default=PROJECT / "dist" / "ImageCompressor.zip")
    arguments = parser.parse_args()
    try:
        archive = create_portable_zip(arguments.source, arguments.output)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.error(str(error))
    print(f"Portable ZIP created: {archive}")


if __name__ == "__main__":
    main()
