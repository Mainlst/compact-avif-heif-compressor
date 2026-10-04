"""Frameless Windows chrome that keeps native taskbar and window behavior."""

from __future__ import annotations

from functools import lru_cache
import sys
import tkinter as tk


class _WindowsChromeAPI:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.rect_type = wintypes.RECT
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetClientRect.restype = wintypes.BOOL
        suffix = "PtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "W"
        self.get_long = getattr(self.user32, "GetWindowLong" + suffix)
        self.get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_long.restype = ctypes.c_ssize_t
        self.set_long = getattr(self.user32, "SetWindowLong" + suffix)
        self.set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.set_long.restype = ctypes.c_ssize_t
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.ReleaseCapture.argtypes = []
        self.user32.ReleaseCapture.restype = wintypes.BOOL
        self.user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.SendMessageW.restype = wintypes.LPARAM
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        self.dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD,
        ]
        self.dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

    def handle(self, root):
        return self.user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()

    def _set_style(self, hwnd, index, value):
        self.ctypes.set_last_error(0)
        previous = self.set_long(hwnd, index, value)
        if previous == 0 and self.ctypes.get_last_error():
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def _dwm(self, hwnd, attribute, value):
        data = self.ctypes.c_uint(value)
        return self.dwmapi.DwmSetWindowAttribute(hwnd, attribute, self.ctypes.byref(data), self.ctypes.sizeof(data))

    def dark_caption(self, root) -> bool:
        hwnd = self.handle(root)
        result = self._dwm(hwnd, 20, 1)
        if result != 0:
            result = self._dwm(hwnd, 19, 1)  # Earlier Windows 10 builds.
        self._dwm(hwnd, 33, 1)
        self._dwm(hwnd, 34, 0x181818)
        self._dwm(hwnd, 35, 0x181818)
        self._dwm(hwnd, 36, 0xCCCCCC)
        return result == 0

    def remove_frame(self, root) -> bool:
        hwnd = self.handle(root)
        if not hwnd or self.user32.IsIconic(hwnd):
            return False
        client = self.rect_type()
        if not self.user32.GetClientRect(hwnd, self.ctypes.byref(client)):
            return False
        style = self.get_long(hwnd, -16) & 0xFFFFFFFF
        extended = self.get_long(hwnd, -20) & 0xFFFFFFFF
        minimum, maximum = root.minsize(), root.maxsize()
        fixed_width = minimum[0] if minimum[0] == maximum[0] else None
        fixed_height = minimum[1] if minimum[1] == maximum[1] else None
        # Keep WS_SYSMENU / WS_MINIMIZEBOX, native ownership and Tk's wrapper.
        # Clearing WS_CAPTION also removes WS_BORDER and WS_DLGFRAME.
        frameless = (style & ~(0x00C00000 | 0x00040000 | 0x00010000)) | 0x000A0000
        taskbar = (extended | 0x00040000) & ~0x00000080
        if style != frameless:
            self._set_style(hwnd, -16, frameless)
        if extended != taskbar:
            self._set_style(hwnd, -20, taskbar)
        # Suppress the non-client frame/shadow. Unsupported newer attributes
        # are harmless on Windows 10.
        self._dwm(hwnd, 2, 1)  # DWMNCRP_DISABLED
        self._dwm(hwnd, 20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        self._dwm(hwnd, 33, 1)  # DWMWA_WINDOW_CORNER_PREFERENCE: DONOTROUND
        self._dwm(hwnd, 34, 0xFFFFFFFE)  # DWMWA_BORDER_COLOR: COLOR_NONE
        width, height = client.right - client.left, client.bottom - client.top
        target_width = fixed_width or width
        target_height = fixed_height or height
        if style != frameless or extended != taskbar or (width, height) != (target_width, target_height):
            # Respect explicit client limits while refreshing the native frame.
            width, height = target_width, target_height
            if not self.user32.SetWindowPos(hwnd, None, 0, 0, width, height, 0x0036):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
        return True

    def is_minimized(self, root) -> bool:
        return bool(self.user32.IsIconic(self.handle(root)))

    def minimize(self, root):
        self.user32.PostMessageW(self.handle(root), 0x0112, 0xF020, 0)  # SC_MINIMIZE

    def drag(self, root, x: int, y: int):
        self.user32.ReleaseCapture()
        point = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        # Start after the Tk callback returns. A synchronous move loop can
        # reenter Tk's Python callbacks without a valid interpreter state.
        self.user32.PostMessageW(self.handle(root), 0x00A1, 2, point)


@lru_cache(maxsize=1)
def _windows_api():
    if sys.platform != "win32":
        return None
    try:
        return _WindowsChromeAPI()
    except (AttributeError, OSError):
        return None


def window_is_minimized(root) -> bool:
    """Cover minimization triggered outside Tk."""
    api = _windows_api()
    if api is not None:
        try:
            return api.is_minimized(root)
        except (AttributeError, OSError, tk.TclError):
            pass
    return root.state() == "iconic"


def dark_native_caption(root) -> bool:
    """Tint secondary native captions without replacing their frame/actions."""
    api = _windows_api()
    if api is None:
        return False
    try:
        root.update_idletasks()
        return api.dark_caption(root)
    except (AttributeError, OSError, tk.TclError):
        return False


class WindowChrome:
    """Actions for an application-drawn caption, native fallback elsewhere."""

    def __init__(self, root, api=None):
        self.root = root
        self._api = _windows_api() if api is None else api
        self.enabled = False
        self._pending = False
        if self._api is not None:
            # Keep Tk's own geometry calculation in sync with the popup frame.
            # Native APPWINDOW/SYSMENU/MINIMIZEBOX below restore taskbar,
            # Alt+Tab and native minimize behavior normally lost by this flag.
            self.root.overrideredirect(True)
        self.refresh()
        if not self.enabled and self._api is not None:
            self.root.overrideredirect(False)
            self.root.update_idletasks()
        if self.enabled:
            # Tk may restore wrapper styles during remapping.
            self.root.bind("<Map>", self._on_map, add="+")

    def refresh(self):
        if self._api is None:
            return False
        try:
            self.root.update_idletasks()
            if self._api.is_minimized(self.root):
                return self.enabled
            self.enabled = self._api.remove_frame(self.root)
        except (AttributeError, OSError, tk.TclError):
            self.enabled = False
        return self.enabled

    def apply(self):
        return self.refresh()

    def _on_map(self, event):
        if event.widget is not self.root or self._pending:
            return
        self._pending = True
        self.root.after_idle(self._refresh_after_map)

    def _refresh_after_map(self):
        self._pending = False
        self.refresh()

    def start_drag(self, event=None):
        if self.enabled and event is not None:
            self._api.drag(self.root, int(event.x_root), int(event.y_root))

    def minimize(self):
        if self.enabled:
            self._api.minimize(self.root)
        else:
            self.root.iconify()

    def is_minimized(self) -> bool:
        if self._api is not None:
            return self._api.is_minimized(self.root)
        return self.root.state() == "iconic"


def install_window_chrome(root) -> WindowChrome:
    return WindowChrome(root)


def compact_caption(root) -> bool:
    """Compatibility shim while older callers switch to custom chrome."""
    return install_window_chrome(root).enabled
