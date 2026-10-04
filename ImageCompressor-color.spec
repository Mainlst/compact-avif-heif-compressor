# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import runpy

project = Path(SPECPATH)
support = runpy.run_path(str(project / "packaging/build_support.py"))
a = Analysis([str(project / "app.py")], **support["analysis_arguments"](project))
support["remove_duplicate_ffmpeg_libraries"](a, project)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ImageCompressor-color", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    icon=str(project / "assets/app-icon.ico"),
)
