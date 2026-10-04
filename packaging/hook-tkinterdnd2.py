"""Bundle drag and drop for the build's operating system and architecture."""

import os
import platform

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("tkinterdnd2", include_py_files=False)

system = platform.system()
machine = os.environ.get("PROCESSOR_ARCHITECTURE", platform.machine()) if system == "Windows" else platform.machine()
platform_directory = {
    ("Windows", "AMD64"): "win-x64",
    ("Windows", "ARM64"): "win-arm64",
    ("Windows", "x86"): "win-x86",
    ("Linux", "x86_64"): "linux-x64",
    ("Linux", "aarch64"): "linux-arm64",
    ("Darwin", "x86_64"): "osx-x64",
    ("Darwin", "arm64"): "osx-arm64",
}.get((system, machine))


def for_current_platform(entry):
    if platform_directory is None:
        return True
    parts = entry[1].replace("\\", "/").split("/")
    if "tkdnd" not in parts:
        return True
    index = parts.index("tkdnd") + 1
    return index >= len(parts) or parts[index] in (platform_directory, platform_directory + "-tcl9")


datas = [entry for entry in datas if for_current_platform(entry)]
binaries = [entry for entry in binaries if for_current_platform(entry)]
