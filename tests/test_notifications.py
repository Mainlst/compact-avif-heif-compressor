"""Completion sounds are asynchronous and cannot break a completed batch."""

from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from compressor.notifications import play_completion_sound


class CompletionSoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SimpleNamespace(bell=Mock())
        self.audio = SimpleNamespace(MessageBeep=Mock(), MB_ICONASTERISK=64)

    def test_windows_queues_system_notification_without_tk_bell(self) -> None:
        with patch("compressor.notifications.sys.platform", "win32"), patch.dict("sys.modules", {"winsound": self.audio}):
            self.assertIsNone(play_completion_sound(self.root))
        self.audio.MessageBeep.assert_called_once_with(64)
        self.root.bell.assert_not_called()

    def test_windows_audio_failure_or_missing_module_uses_tk_bell(self) -> None:
        for failure in (OSError("no audio device"), RuntimeError("sound unavailable")):
            with self.subTest(failure=failure), patch("compressor.notifications.sys.platform", "win32"), patch.dict(
                "sys.modules", {"winsound": self.audio},
            ):
                self.audio.MessageBeep.side_effect = failure
                self.root.bell.reset_mock()
                self.assertIsNone(play_completion_sound(self.root))
                self.root.bell.assert_called_once_with()
        self.root.bell.reset_mock()
        with patch("compressor.notifications.sys.platform", "win32"), patch.dict("sys.modules", {"winsound": None}):
            self.assertIsNone(play_completion_sound(self.root))
        self.root.bell.assert_called_once_with()

    def test_non_windows_uses_tk_bell_without_loading_windows_audio(self) -> None:
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch("compressor.notifications.sys.platform", platform), patch.dict(
                "sys.modules", {"winsound": self.audio},
            ):
                self.root.bell.reset_mock()
                self.assertIsNone(play_completion_sound(self.root))
                self.root.bell.assert_called_once_with()
        self.audio.MessageBeep.assert_not_called()

    def test_failed_fallback_and_closed_tk_window_never_raise(self) -> None:
        for failure in (OSError("no audio device"), RuntimeError("interpreter unavailable"), tk.TclError("application destroyed")):
            for platform in ("linux", "win32"):
                with self.subTest(failure=failure, platform=platform), patch("compressor.notifications.sys.platform", platform), patch.dict(
                    "sys.modules", {"winsound": None},
                ):
                    self.root.bell.side_effect = failure
                    self.assertIsNone(play_completion_sound(self.root))


if __name__ == "__main__":
    unittest.main()
