"""Shared inputs for the portable-folder and single-file Windows builds."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


def analysis_arguments(project: Path) -> dict:
    datas = [
        (str(project / "assets/app-icon.ico"), "assets"),
        (str(project / "assets/app-icon.png"), "assets"),
        (str(project / "vendor/heif"), "vendor/heif"),
        (str(project / "vendor/ffmpeg/windows"), "vendor/ffmpeg/windows"),
        (str(project / "vendor/ffmpeg/licenses"), "vendor/ffmpeg/licenses"),
        (str(project / "vendor/ffmpeg/rebuild-minimal.sh"), "vendor/ffmpeg"),
        (str(project / "vendor/ffmpeg/sources/SHA256SUMS"), "vendor/ffmpeg/sources"),
    ]
    for directory, patterns in (
        ("ffmpeg", ("LICENSE.txt", "COPYING.*", "NOTICE.txt", "manifest.json")),
        ("libavif", ("LICENSE-*", "PATENTS-*.txt", "NOTICE.txt", "manifest.json")),
    ):
        datas.extend((str(project / "vendor" / directory / pattern), f"vendor/{directory}") for pattern in patterns)
    binaries = [(str(project / "vendor/libavif/windows/avifenc.exe"), "vendor/libavif/windows")]
    hiddenimports = []
    for package in ("PIL", "pillow_heif"):
        package_datas, package_binaries, package_imports = collect_all(package, include_py_files=False)
        datas.extend(package_datas)
        binaries.extend(package_binaries)
        hiddenimports.extend(package_imports)
    return dict(
        pathex=[str(project)], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
        hookspath=[str(project / "packaging")], hooksconfig={}, runtime_hooks=[],
        excludes=[], noarchive=False, optimize=0,
    )


def remove_duplicate_ffmpeg_libraries(analysis, project: Path) -> None:
    """FFmpeg loads its adjacent DLLs; it needs no second copy beside Python."""
    library_paths = {file.name.casefold(): file.resolve() for file in (project / "vendor/ffmpeg/windows").glob("*.dll")}
    destinations = {entry[0].replace("\\", "/").casefold() for entry in (*analysis.binaries, *analysis.datas)}
    retained = []
    removed = []
    for entry in analysis.binaries:
        destination, source, _kind = entry
        filename = destination.casefold()
        vendor_copy = f"vendor/ffmpeg/windows/{filename}"
        if filename in library_paths and vendor_copy in destinations and Path(source).resolve() == library_paths[filename]:
            removed.append(destination)
        else:
            retained.append(entry)
    analysis.binaries = retained
    print(f"Removed {len(removed)} duplicate FFmpeg DLL entries; adjacent vendor copies retained.")
