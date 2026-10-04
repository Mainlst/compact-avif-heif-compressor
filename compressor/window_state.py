"""Remember the main window position without saving minimized coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import sys
import tkinter as tk

from .window_chrome import window_is_minimized


@dataclass(frozen=True)
class WindowPosition:
    x: int
    y: int

    @classmethod
    def from_settings(cls, value: object) -> WindowPosition | None:
        if not isinstance(value, dict):
            return None
        x, y = value.get("x"), value.get("y")
        # bool is an int subclass; it is not a usable saved coordinate.
        if type(x) is not int or type(y) is not int:
            return None
        if abs(x) > 1_000_000 or abs(y) > 1_000_000:
            return None
        return cls(x, y)

    def to_settings(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class WorkArea:
    left: int
    top: int
    right: int
    bottom: int


def visible_position(
    position: WindowPosition, width: int, height: int, work_areas: list[WorkArea],
) -> WindowPosition:
    """Keep the whole window on the nearest available monitor work area.

    Negative coordinates are normal for monitors above or left of the primary
    display. If a monitor has disappeared, prefer the remaining monitor with
    most overlap, then the shortest distance to a valid placement.
    """
    areas = [area for area in work_areas if area.right > area.left and area.bottom > area.top]
    if not areas:
        return position
    width, height = max(1, width), max(1, height)

    def placement(area: WorkArea) -> WindowPosition:
        # For an unusually small display, leave the title bar reachable even
        # when the application itself cannot fit inside the available area.
        x = min(max(position.x, area.left), max(area.left, area.right - width))
        y = min(max(position.y, area.top), max(area.top, area.bottom - height))
        return WindowPosition(x, y)

    def score(area: WorkArea) -> tuple[int, int]:
        overlap_width = max(0, min(position.x + width, area.right) - max(position.x, area.left))
        overlap_height = max(0, min(position.y + height, area.bottom) - max(position.y, area.top))
        candidate = placement(area)
        distance = (candidate.x - position.x) ** 2 + (candidate.y - position.y) ** 2
        return overlap_width * overlap_height, -distance

    return placement(max(areas, key=score))


class _WindowsAPI:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
            ]

        self.ctypes = ctypes
        self.rect_type = wintypes.RECT
        self.monitor_info_type = MonitorInfo
        self.callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), ctypes.c_ssize_t,
        )
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL
        self.user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(wintypes.RECT), self.callback_type, ctypes.c_ssize_t,
        ]
        self.user32.EnumDisplayMonitors.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL

    def handle(self, root: tk.Tk):
        # Tk's ID names its client window; the native wrapper owns the caption.
        return self.user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()

    def frame(self, root: tk.Tk) -> tuple[WindowPosition, int, int] | None:
        rect = self.rect_type()
        if not self.user32.GetWindowRect(self.handle(root), self.ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        return WindowPosition(rect.left, rect.top), width, height

    def work_areas(self) -> list[WorkArea]:
        areas = []

        def collect(monitor, _dc, _rect, _data):
            info = self.monitor_info_type()
            info.cbSize = self.ctypes.sizeof(info)
            if self.user32.GetMonitorInfoW(monitor, self.ctypes.byref(info)):
                rect = info.rcWork
                areas.append(WorkArea(rect.left, rect.top, rect.right, rect.bottom))
            return True

        callback = self.callback_type(collect)
        self.user32.EnumDisplayMonitors(None, None, callback, 0)
        return areas

    def move(self, root: tk.Tk, position: WindowPosition) -> bool:
        # Preserve the fixed client size, Z order and active application.
        flags = 0x0001 | 0x0004 | 0x0010 | 0x0200
        return bool(self.user32.SetWindowPos(self.handle(root), None, position.x, position.y, 0, 0, flags))


@lru_cache(maxsize=1)
def _windows_api() -> _WindowsAPI | None:
    if sys.platform != "win32":
        return None
    try:
        return _WindowsAPI()
    except (AttributeError, OSError):
        return None


def _window_frame(root: tk.Tk) -> tuple[WindowPosition, int, int]:
    api = _windows_api()
    if api is not None:
        frame = api.frame(root)
        if frame is not None:
            return frame
    return WindowPosition(root.winfo_x(), root.winfo_y()), root.winfo_width(), root.winfo_height()


def _work_areas(root: tk.Tk) -> list[WorkArea]:
    api = _windows_api()
    if api is not None:
        areas = api.work_areas()
        if areas:
            return areas
    left, top = root.winfo_vrootx(), root.winfo_vrooty()
    return [WorkArea(left, top, left + root.winfo_vrootwidth(), top + root.winfo_vrootheight())]


def _move_window(root: tk.Tk, position: WindowPosition):
    if root.state() == "withdrawn":
        # Tk recreates its native wrapper when first mapping it. Store the
        # position in Tk as well, so the first visible frame starts here.
        root.geometry(f"+{position.x}+{position.y}")
        return
    api = _windows_api()
    if api is not None and api.move(root, position):
        return
    # "+-100" means the absolute coordinate -100 in Tk. A bare "-100"
    # instead measures the distance from the right/bottom screen edge.
    root.geometry(f"+{position.x}+{position.y}")


class WindowPositionTracker:
    """Track normal positions and restore saved coordinates after UI setup."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self._saved_position: WindowPosition | None = None
        self._last_position: WindowPosition | None = None
        self.root.bind("<Configure>", self._on_configure, add="+")

    def load(self, value: object):
        # Keep the requested restore position separate from Configure events
        # generated while creating widgets and changing the window's caption.
        self._saved_position = WindowPosition.from_settings(value)

    def capture(self) -> WindowPosition | None:
        try:
            if self.root.state() == "normal" and not window_is_minimized(self.root):
                self._last_position = _window_frame(self.root)[0]
        except (tk.TclError, OSError):
            pass
        return self._last_position

    def restore(self) -> WindowPosition | None:
        try:
            self.root.update_idletasks()
            if self._saved_position is not None:
                _position, width, height = _window_frame(self.root)
                position = visible_position(self._saved_position, width, height, _work_areas(self.root))
                _move_window(self.root, position)
                self._last_position = position
                self.root.update_idletasks()
        except (tk.TclError, OSError):
            pass
        self._saved_position = None
        return self.capture()

    def to_settings(self) -> dict[str, int] | None:
        position = self.capture()
        return position.to_settings() if position is not None else None

    def _on_configure(self, event):
        if event.widget is self.root:
            self.capture()
