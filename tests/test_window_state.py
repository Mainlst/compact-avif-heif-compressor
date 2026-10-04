"""Window placement validation and minimized-window persistence."""

import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from compressor.window_state import WindowPosition, WindowPositionTracker, WorkArea, visible_position


class PlacementTests(unittest.TestCase):
    def test_position_settings_accept_negative_monitor_coordinates(self):
        position = WindowPosition.from_settings({"x": -1280, "y": -200})
        self.assertEqual(position, WindowPosition(-1280, -200))
        self.assertEqual(position.to_settings(), {"x": -1280, "y": -200})

    def test_corrupt_position_settings_are_ignored(self):
        invalid = [None, [], "100,100", {}, {"x": 1}, {"x": True, "y": 10},
                   {"x": 10, "y": False}, {"x": 1.2, "y": 10},
                   {"x": "10", "y": 10}, {"x": 10 ** 20, "y": 10}]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(WindowPosition.from_settings(value))

    def test_restore_preserves_visible_position_on_negative_coordinate_monitor(self):
        areas = [WorkArea(0, 0, 1920, 1040), WorkArea(-1280, -200, 0, 824)]
        position = WindowPosition(-1100, -100)
        self.assertEqual(visible_position(position, 200, 320, areas), position)

    def test_restore_clamps_to_work_area_above_taskbar(self):
        areas = [WorkArea(0, 0, 1920, 1040)]
        self.assertEqual(visible_position(WindowPosition(1880, 1000), 200, 320, areas), WindowPosition(1720, 720))

    def test_removed_monitor_restores_to_nearest_remaining_monitor(self):
        areas = [WorkArea(0, 0, 1920, 1040), WorkArea(1920, 0, 3840, 1040)]
        self.assertEqual(visible_position(WindowPosition(4100, 100), 200, 320, areas), WindowPosition(3640, 100))
        self.assertEqual(visible_position(WindowPosition(-1100, -200), 200, 320, areas), WindowPosition(0, 0))

    def test_spanning_window_prefers_monitor_with_more_overlap(self):
        areas = [WorkArea(-1280, 0, 0, 1024), WorkArea(0, 0, 1920, 1040)]
        self.assertEqual(visible_position(WindowPosition(-150, 100), 200, 320, areas), WindowPosition(-200, 100))

    def test_tiny_display_keeps_title_bar_reachable(self):
        areas = [WorkArea(-100, -100, 0, 0)]
        self.assertEqual(visible_position(WindowPosition(-200, -200), 200, 320, areas), WindowPosition(-100, -100))

    def test_unavailable_or_invalid_work_area_does_not_destroy_saved_position(self):
        position = WindowPosition(50, 100)
        self.assertEqual(visible_position(position, 200, 320, []), position)
        self.assertEqual(visible_position(position, 200, 320, [WorkArea(0, 0, 0, 0)]), position)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.root = Mock()
        self.root.state.return_value = "normal"
        self.frame = (WindowPosition(100, 200), 200, 320)
        frame_patch = patch("compressor.window_state._window_frame", side_effect=lambda _root: self.frame)
        self.frame_mock = frame_patch.start()
        self.addCleanup(frame_patch.stop)
        minimized_patch = patch("compressor.window_state.window_is_minimized", return_value=False)
        self.minimized_mock = minimized_patch.start()
        self.addCleanup(minimized_patch.stop)
        self.tracker = WindowPositionTracker(self.root)

    def test_minimized_close_keeps_last_normal_position(self):
        self.tracker._on_configure(SimpleNamespace(widget=self.root))
        self.root.state.return_value = "iconic"
        self.frame = (WindowPosition(-32000, -32000), 160, 28)
        self.tracker._on_configure(SimpleNamespace(widget=self.root))
        self.assertEqual(self.tracker.to_settings(), {"x": 100, "y": 200})

    def test_native_minimization_keeps_position_even_if_tk_reports_normal(self):
        self.tracker.capture()
        self.minimized_mock.return_value = True
        self.frame = (WindowPosition(-32000, -32000), 160, 28)
        self.assertEqual(self.tracker.to_settings(), {"x": 100, "y": 200})

    def test_child_configure_does_not_overwrite_main_position(self):
        self.tracker.capture()
        self.frame = (WindowPosition(300, 400), 200, 320)
        self.tracker._on_configure(SimpleNamespace(widget=object()))
        self.root.state.return_value = "iconic"
        self.assertEqual(self.tracker.to_settings(), {"x": 100, "y": 200})

    def test_setup_events_do_not_overwrite_saved_restore_position(self):
        self.tracker.load({"x": 4000, "y": 100})
        self.tracker._on_configure(SimpleNamespace(widget=self.root))

        def move(_root, position):
            self.frame = (position, 200, 320)

        with patch("compressor.window_state._work_areas", return_value=[WorkArea(0, 0, 1920, 1040)]), \
                patch("compressor.window_state._move_window", side_effect=move) as moved:
            self.assertEqual(self.tracker.restore(), WindowPosition(1720, 100))
        moved.assert_called_once_with(self.root, WindowPosition(1720, 100))
        self.assertEqual(self.tracker.to_settings(), {"x": 1720, "y": 100})

    def test_no_saved_position_preserves_default_placement(self):
        with patch("compressor.window_state._move_window") as moved:
            self.assertEqual(self.tracker.restore(), WindowPosition(100, 200))
        moved.assert_not_called()

    def test_destroyed_root_retains_last_known_position(self):
        self.tracker.capture()
        self.root.state.side_effect = tk.TclError("window was destroyed")
        self.assertEqual(self.tracker.to_settings(), {"x": 100, "y": 200})


if __name__ == "__main__":
    unittest.main()
