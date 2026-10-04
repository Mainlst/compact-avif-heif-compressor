"""Best-effort completion sounds without blocking the conversion UI."""

from __future__ import annotations

import sys
from typing import Protocol


class _BellWindow(Protocol):
    def bell(self) -> None: ...


def play_completion_sound(root: _BellWindow) -> None:
    """Queue a Windows system sound, or use the window's bell.

    Call this on the Tk thread. Audio availability must never affect the
    completed batch, and a closed or unavailable Tcl interpreter is harmless.
    """
    if sys.platform == "win32":
        try:
            # Keeping the import here avoids loading audio support at startup;
            # its explicit name also lets PyInstaller collect the stdlib module.
            import winsound

            # Windows queues playback independently of this process, allowing
            # the app to exit immediately after the completed batch.
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        except (ImportError, OSError, RuntimeError):
            pass

    try:
        root.bell()
    except Exception:
        # TclError, absent audio devices and already-destroyed windows are all
        # notification failures. Do not import Tk solely to classify its error.
        pass
