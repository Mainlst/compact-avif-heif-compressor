"""Custom-caption actions and native minimized-state handling."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from compressor.window_chrome import WindowChrome, dark_native_caption, window_is_minimized


class WindowChromeTests(unittest.TestCase):
    def setUp(self):
        self.root = Mock()
        self.root.state.return_value = "normal"
        self.api = Mock()
        self.api.is_minimized.return_value = False
        self.api.remove_frame.return_value = True
        self.chrome = WindowChrome(self.root, self.api)

    def test_drag_passes_screen_coordinates_to_native_move_loop(self):
        self.chrome.start_drag(SimpleNamespace(x_root=-120, y_root=200))
        self.api.drag.assert_called_once_with(self.root, -120, 200)

    def test_caption_minimize_uses_native_window(self):
        self.chrome.minimize()
        self.api.minimize.assert_called_once_with(self.root)
        self.root.iconify.assert_not_called()

    def test_restore_map_reapplies_chrome_only_for_main_window(self):
        self.chrome._on_map(SimpleNamespace(widget=object()))
        self.root.after_idle.assert_not_called()
        self.chrome._on_map(SimpleNamespace(widget=self.root))
        self.chrome._on_map(SimpleNamespace(widget=self.root))
        self.root.after_idle.assert_called_once()
        self.chrome._refresh_after_map()
        self.assertEqual(self.api.remove_frame.call_count, 2)

    def test_refresh_never_resizes_a_minimized_window(self):
        self.api.is_minimized.return_value = True
        self.chrome.refresh()
        self.assertTrue(self.chrome.enabled)
        self.assertEqual(self.api.remove_frame.call_count, 1)

    def test_unavailable_native_chrome_keeps_normal_minimize(self):
        with patch("compressor.window_chrome._windows_api", return_value=None):
            chrome = WindowChrome(self.root)
        self.assertFalse(chrome.enabled)
        chrome.minimize()
        self.root.iconify.assert_called_once()

    def test_failed_native_install_does_not_enable_custom_caption(self):
        self.api.remove_frame.side_effect = OSError("not supported")
        chrome = WindowChrome(self.root, self.api)
        self.assertFalse(chrome.enabled)
        self.root.overrideredirect.assert_has_calls([call(True), call(False)])
        chrome.start_drag(SimpleNamespace(x_root=20, y_root=20))
        self.api.drag.assert_not_called()

    def test_native_iconic_state_wins_when_tk_state_is_stale(self):
        self.api.is_minimized.return_value = True
        with patch("compressor.window_chrome._windows_api", return_value=self.api):
            self.assertTrue(window_is_minimized(self.root))
        self.root.state.assert_not_called()

    def test_secondary_caption_tint_does_not_remove_its_frame(self):
        self.api.remove_frame.reset_mock()
        self.api.dark_caption.return_value = True
        with patch("compressor.window_chrome._windows_api", return_value=self.api):
            self.assertTrue(dark_native_caption(self.root))
        self.api.dark_caption.assert_called_once_with(self.root)
        self.api.remove_frame.assert_not_called()


if __name__ == "__main__":
    unittest.main()
