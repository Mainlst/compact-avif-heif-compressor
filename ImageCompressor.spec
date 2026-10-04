# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import runpy

project = Path(SPECPATH)
support = runpy.run_path(str(project / "packaging/build_support.py"))
a = Analysis([str(project / "app.py")], **support["analysis_arguments"](project))
support["remove_duplicate_ffmpeg_libraries"](a, project)
pyz = PYZ(a.pure)

# Keep libraries beside the launcher, so startup needs no temporary extraction.
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="ImageCompressor", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    icon=str(project / "assets/app-icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ImageCompressor")
