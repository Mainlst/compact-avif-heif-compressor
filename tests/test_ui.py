"""Exercise the desktop workflow and queue handling with a real Tk window.

On Linux, use xvfb-run -a python3 -m unittest discover -s tests -v.
"""

from pathlib import Path
import gc
import tempfile
import json
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from compressor.engine import convert_image
from compressor.ui import COMPACT_CLIENT_SIZE, CompressorApp, FORMAT_LABELS, PRESERVE_PRESET, SIZES, create_root


class StartupVisibilityTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings_file = Path(temporary.name) / "settings.json"
        configuration = patch("compressor.ui.settings_path", return_value=self.settings_file)
        configuration.start()
        self.addCleanup(configuration.stop)

    def root(self):
        try:
            root, dnd = create_root()
        except tk.TclError as error:
            self.skipTest(f"No desktop display: {error}")
        def cleanup():
            try:
                root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(cleanup)
        return root, dnd

    def test_first_map_has_final_size_and_saved_position(self):
        self.settings_file.write_text(json.dumps({"window_position": {"x": 340, "y": 210}}), encoding="utf-8")
        root, dnd = self.root()
        appearances = []

        def mapped(event):
            if event.widget is root:
                appearances.append((root.winfo_width(), root.winfo_height(), root.winfo_x(), root.winfo_y()))

        root.bind("<Map>", mapped, add="+")
        root.update()
        self.assertFalse(root.winfo_viewable(), "The default Tk window must stay hidden during startup")
        self.assertEqual(appearances, [])
        app = CompressorApp(root, dnd)
        root.update()
        self.assertTrue(root.winfo_viewable())
        self.assertTrue(appearances)
        self.assertEqual(set(appearances), {(196, 328, 340, 210)})
        self.assertEqual(app.advanced_window.state(), "withdrawn")
        self.assertEqual(app.history_window.state(), "withdrawn")
        app.close()

    def test_missing_drag_and_drop_keeps_the_fallback_window_hidden(self):
        with patch.dict("sys.modules", {"tkinterdnd2": None}):
            root, dnd = self.root()
        root.update()
        self.assertFalse(dnd)
        self.assertEqual(root.state(), "withdrawn")
        self.assertFalse(root.winfo_viewable())


class DesktopWorkflowTests(unittest.TestCase):
    def setUp(self):
        # Each test creates a Tcl interpreter. Collect previous test windows
        # here, so image-worker GC cannot finalize their Tk variables.
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config_patch = patch("compressor.ui.settings_path", return_value=self.directory / "settings.json")
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        try:
            self.root, dnd = create_root()
        except tk.TclError as error:
            self.skipTest(f"No desktop display: {error}")
        self.app = CompressorApp(self.root, dnd)
        self.addCleanup(self.close_window)
        self.root.update()

    def close_window(self):
        if self.app.busy:
            self.app.cancel()
            self.wait_for(lambda: not self.app.busy)
        self.app.close()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Desktop workflow did not finish before timeout")

    def image(self, name):
        path = self.directory / name
        with Image.new("RGBA", (64, 32), (120, 60, 200, 128)) as image:
            image.save(path)
        return path

    def test_drop_handles_japanese_spaces_and_braces_and_continues_after_error(self):
        initial_size = (self.root.winfo_width(), self.root.winfo_height())
        files = [self.image("写真 {new}.png"), self.image("space name.png")]
        corrupt = self.directory / "broken.png"
        corrupt.write_bytes(b"not an image")
        files.insert(1, corrupt)
        tcl_list = self.root.tk.call("format", "%s", self.root.tk.call("list", *map(str, files)))
        self.assertEqual(self.app._drop(SimpleNamespace(data=tcl_list)), "copy")
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(len(self.app.results), 2)
        self.assertEqual(len(self.app.failures), 1)
        self.assertEqual(len(self.app.history.get_children()), 3)
        self.assertTrue(self.app.history_visible)
        self.root.update()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), initial_size)
        self.assertIsNot(self.app.history.winfo_toplevel(), self.root)
        self.assertIn("2枚完了", self.app.status.get())
        self.assertIn("1枚失敗", self.app.status.get())
        for result in self.app.results:
            with Image.open(result.output_path) as encoded:
                self.assertEqual(encoded.format, self.app._selected_format())
                self.assertEqual(encoded.size, (64, 32))
        self.assertEqual(corrupt.read_bytes(), b"not an image")

    def test_empty_new_batch_clears_previous_destination(self):
        self.app.start([self.image("source.png")])
        self.wait_for(lambda: not self.app.busy)
        self.assertIsNotNone(self.app.last_output)
        self.assertFalse(self.app.open_button.instate(["disabled"]))
        self.app.start([self.directory / "missing.png"])
        self.wait_for(lambda: not self.app.busy)
        self.assertIsNone(self.app.last_output)
        self.assertTrue(self.app.open_button.instate(["disabled"]))
        self.assertEqual(len(self.app.results), 0)

    def test_cancel_finishes_current_image_and_ui_stays_responsive(self):
        files = [self.image(f"{index}.png") for index in range(3)]
        self.app.completion_sound.set(True)
        self.app.auto_close.set(True)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow_convert(source, settings, output_dir):
            calls.append(source)
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test encoder release timed out")
            return convert_image(source, settings, output_dir)

        with patch("compressor.ui.convert_image", side_effect=slow_convert), patch("compressor.ui.play_completion_sound") as sound:
            self.app.start(files)
            try:
                self.wait_for(entered.is_set)
                self.assertTrue(self.app.browse_button.instate(["disabled"]))
                self.assertTrue(self.app.overwrite_check.instate(["disabled"]))
                self.assertEqual(self.app._drop(SimpleNamespace(data=str(files[0]))), "refuse_drop")
                ticked = []
                self.root.after(10, lambda: ticked.append(True))
                self.wait_for(lambda: bool(ticked))
                self.app.cancel()
                self.assertTrue(self.app.cancel_button.instate(["disabled"]))
            finally:
                release.set()
            self.wait_for(lambda: not self.app.busy)
            sound.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.app.results), 1)
        self.assertIn("中止", self.app.status.get())
        self.assertFalse(self.app.browse_button.instate(["disabled"]))
        self.assertFalse(self.app.overwrite_check.instate(["disabled"]))
        self.assertFalse(self.app.closing)

    def test_compact_main_keeps_controls_visible_when_history_opens(self):
        initial_size = (self.root.winfo_width(), self.root.winfo_height())
        initial_minimum = self.root.minsize()
        self.assertEqual(initial_size, COMPACT_CLIENT_SIZE)
        self.assertEqual(initial_size, (196, 328))
        self.assertEqual(initial_minimum, initial_size)
        self.assertEqual(self.root.resizable(), (0, 0))
        bounds = (
            self.root.winfo_rootx(), self.root.winfo_rooty(),
            self.root.winfo_rootx() + initial_size[0],
            self.root.winfo_rooty() + initial_size[1],
        )

        def check_main_widgets(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, tk.Toplevel):
                    continue
                if widget.winfo_ismapped():
                    with self.subTest(widget=str(widget)):
                        self.assertGreaterEqual(widget.winfo_rootx(), bounds[0])
                        self.assertGreaterEqual(widget.winfo_rooty(), bounds[1])
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), bounds[2])
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bounds[3])
                        # A control may fit the window while extending past
                        # its own panel, which also clips the visible content.
                        self.assertGreaterEqual(widget.winfo_x(), 0)
                        self.assertGreaterEqual(widget.winfo_y(), 0)
                        self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), parent.winfo_width())
                        self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), parent.winfo_height())
                check_main_widgets(widget)

        check_main_widgets(self.root)
        for widget in (self.app.browse_button, self.app.status_label, self.app.cancel_button):
            self.assertTrue(widget.winfo_ismapped())
        self.app.toggle_history()
        self.root.update()
        self.assertIsNot(self.app.history.winfo_toplevel(), self.root)
        self.assertTrue(self.app.history.winfo_ismapped())
        self.assertTrue(self.app.summary_label.winfo_ismapped())
        self.assertTrue(self.app.cancel_button.winfo_ismapped())
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), initial_size)
        self.assertEqual(self.root.minsize(), initial_minimum)
        self.app.toggle_history()
        self.root.update()
        self.assertFalse(self.app.history.winfo_ismapped())
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), initial_size)
        self.assertEqual(self.root.minsize()[1], self.app.minimum_height)

    def test_png_quality_controls_and_avif_speed_controls(self):
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app._format_changed()
        self.assertTrue(self.app.preset_combo.instate(["disabled"]))
        self.assertTrue(self.app.quality_scale.instate(["disabled"]))
        self.assertTrue(self.app.speed_combo.instate(["disabled"]))
        if "AVIF" in self.app.formats:
            self.app.output_format.set(FORMAT_LABELS["AVIF"])
            self.app._format_changed()
            self.assertTrue(self.app.quality_scale.instate(["disabled"]))
            self.assertTrue(self.app.size_combo.instate(["disabled"]))
            self.assertFalse(self.app.speed_combo.instate(["disabled"]))
            self.app.preserve_image.set(False)
            self.app._preservation_changed()
            self.assertFalse(self.app.quality_scale.instate(["disabled"]))
            self.assertFalse(self.app.size_combo.instate(["disabled"]))

    def test_older_lossy_settings_migrate_to_preserving_pixels_and_dimensions(self):
        settings = self.directory / "settings.json"
        settings.write_text(json.dumps({"format": "JPEG", "quality": 40, "size": "長辺 960 px"}), encoding="utf-8")
        self.app._load_settings()
        self.app._refresh_controls()
        self.assertTrue(self.app.preserve_image.get())
        self.assertNotEqual(self.app._selected_format(), "JPEG")
        self.assertEqual(SIZES[self.app.max_size.get()], 100)
        self.assertFalse(self.app.overwrite_original.get())
        self.assertTrue(self.app.quality_scale.instate(["disabled"]))
        self.assertTrue(self.app.size_combo.instate(["disabled"]))
        self.assertNotIn(FORMAT_LABELS["JPEG"], self.app.format_combo.cget("values"))
        self.app._save_settings()
        self.assertIs(json.loads(settings.read_text(encoding="utf-8"))["preserve_image"], True)

    def test_main_preset_switches_lossy_and_lossless_without_opening_details(self):
        if "AVIF" not in self.app.formats:
            self.skipTest("AVIF encoder is not installed")
        initial_size = (self.root.winfo_width(), self.root.winfo_height())
        self.app.output_format.set(FORMAT_LABELS["AVIF"])
        self.app._format_changed()
        self.assertFalse(self.app.preset_combo.instate(["disabled"]))
        self.app.preset.set("高画質")
        self.app.preset_combo.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertFalse(self.app.preserve_image.get())
        self.assertEqual(self.app.quality.get(), 85)
        self.assertFalse(self.app.quality_scale.instate(["disabled"]))
        self.assertIn(FORMAT_LABELS["JPEG"], self.app.format_combo.cget("values"))
        self.app.start([self.image("lossy from preset.png")])
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(len(self.app.results), 1)
        self.assertEqual(len(self.app.failures), 0)
        self.app.preset.set(PRESERVE_PRESET)
        self.app.preset_combo.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertTrue(self.app.preserve_image.get())
        self.assertTrue(self.app.quality_scale.instate(["disabled"]))
        self.assertEqual(SIZES[self.app.max_size.get()], 100)
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), initial_size)

    def test_settings_are_saved_outside_image_folder_and_reloaded(self):
        self.app.preserve_image.set(False)
        self.app.output_format.set(FORMAT_LABELS["JPEG"])
        self.app.quality.set(82)
        self.app.max_size.set("50%")
        self.app.folder_mode.set("custom")
        self.app.folder.set(str(self.directory / "output"))
        self.app.keep_metadata.set(True)
        self.app._save_settings()
        self.app.quality.set(65)
        self.app.max_size.set("25%")
        self.app.folder_mode.set("same")
        self.app.keep_metadata.set(False)
        self.app._load_settings()
        self.assertEqual(self.app._selected_format(), "JPEG")
        self.assertEqual(self.app.quality.get(), 82)
        self.assertEqual(self.app._selected_scale_percent(), 50)
        self.assertEqual(self.app.folder_mode.get(), "custom")
        self.assertTrue(self.app.keep_metadata.get())

    def test_resize_percentages_apply_to_original_width_and_height(self):
        self.app.preserve_image.set(False)
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app.folder_mode.set("custom")
        self.app.folder.set(str(self.directory / "resized"))
        self.app._format_changed()
        self.assertFalse(self.app.size_combo.instate(["disabled"]))
        source = self.image("percentage.png")
        original = source.read_bytes()
        for label, expected_size in (
            ("75%", (48, 24)), ("50%", (32, 16)),
            ("25%", (16, 8)), ("37", (24, 12)),
        ):
            with self.subTest(percentage=label):
                self.app.max_size.set(label)
                self.app.start([source])
                self.wait_for(lambda: not self.app.busy)
                self.assertEqual(self.app.failures, [])
                result = self.app.results[0]
                self.assertEqual((result.width, result.height), expected_size)
                with Image.open(result.output_path) as image:
                    self.assertEqual(image.size, expected_size)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (196, 328))

    def test_percentage_and_original_replacement_settings_are_reloaded(self):
        self.app.preserve_image.set(False)
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app.max_size.set("37%")
        self.app.folder_mode.set("custom")
        self.app.folder.set(str(self.directory / "custom"))
        self.app._refresh_controls()
        self.app.overwrite_check.invoke()
        self.app._save_settings()
        saved = json.loads((self.directory / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["scale_percent"], 37)
        self.assertIs(saved["overwrite_original"], True)
        self.app.max_size.set("25%")
        self.app.overwrite_original.set(False)
        self.app._load_settings()
        self.app._refresh_controls()
        self.assertEqual(self.app._selected_scale_percent(), 37)
        self.assertTrue(self.app.overwrite_original.get())
        self.assertEqual(self.app.folder_mode.get(), "same")
        self.assertTrue(self.app.folder_entry.instate(["disabled"]))

    def test_original_replacement_checkbox_disables_destination_controls(self):
        self.assertIs(self.app.overwrite_check.winfo_toplevel(), self.root)
        self.assertTrue(self.app.overwrite_check.winfo_ismapped())
        self.assertEqual(self.app.advanced_window.state(), "withdrawn")
        self.app.folder_mode.set("custom")
        self.app._refresh_controls()
        self.assertFalse(self.app.folder_entry.instate(["disabled"]))
        self.app.overwrite_check.invoke()
        self.assertTrue(self.app.overwrite_original.get())
        saved = json.loads((self.directory / "settings.json").read_text(encoding="utf-8"))
        self.assertIs(saved["overwrite_original"], True)
        self.assertEqual(self.app.folder_mode.get(), "same")
        for widget in (self.app.same_radio, self.app.custom_radio, self.app.folder_entry, self.app.folder_button):
            self.assertTrue(widget.instate(["disabled"]))
        self.app.overwrite_check.invoke()
        self.assertFalse(self.app.overwrite_original.get())
        saved = json.loads((self.directory / "settings.json").read_text(encoding="utf-8"))
        self.assertIs(saved["overwrite_original"], False)
        self.assertFalse(self.app.same_radio.instate(["disabled"]))
        self.assertFalse(self.app.custom_radio.instate(["disabled"]))
        self.app.folder_mode.set("custom")
        self.app._refresh_controls()
        self.assertFalse(self.app.folder_entry.instate(["disabled"]))
        self.assertFalse(self.app.folder_button.instate(["disabled"]))
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (196, 328))

    def test_completion_preferences_are_saved_immediately_and_reloaded(self):
        names = ("completion_sound", "auto_close", "always_on_top")
        self.app.show_advanced()
        self.root.update()
        for name in names:
            with self.subTest(preference=name):
                variable = getattr(self.app, name)
                checkbox = getattr(self.app, f"{name}_check")
                self.assertFalse(variable.get())
                self.assertIs(checkbox.winfo_toplevel(), self.app.advanced_window)
                self.assertTrue(checkbox.winfo_ismapped())
                checkbox.invoke()
                saved = json.loads((self.directory / "settings.json").read_text(encoding="utf-8"))
                self.assertIs(saved[name], True)
                self.assertTrue(variable.get())
        for name in names:
            getattr(self.app, name).set(False)
        self.app._load_settings()
        for name in names:
            self.assertTrue(getattr(self.app, name).get())
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), COMPACT_CLIENT_SIZE)

    def test_completion_preferences_default_off_and_accept_only_boolean_true(self):
        names = ("completion_sound", "auto_close", "always_on_top")
        settings = self.directory / "settings.json"
        for name in names:
            self.assertFalse(getattr(self.app, name).get())
        for value in (None, False, "true", 1, [True]):
            with self.subTest(value=value):
                for name in names:
                    getattr(self.app, name).set(True)
                data = {} if value is None else dict.fromkeys(names, value)
                settings.write_text(json.dumps(data), encoding="utf-8")
                self.app._load_settings()
                for name in names:
                    self.assertFalse(getattr(self.app, name).get())

    def test_always_on_top_updates_main_and_auxiliary_windows_immediately(self):
        windows = (self.root, self.app.advanced_window, self.app.history_window)
        self.app.show_advanced()
        self.app.toggle_history()
        self.root.update()
        with patch.object(windows[0], "attributes", wraps=windows[0].attributes) as main_attributes, \
                patch.object(windows[1], "attributes", wraps=windows[1].attributes) as advanced_attributes, \
                patch.object(windows[2], "attributes", wraps=windows[2].attributes) as history_attributes:
            for enabled in (True, False):
                self.app.always_on_top_check.invoke()
                self.root.update()
                self.assertIs(self.app.always_on_top.get(), enabled)
                for attributes in (main_attributes, advanced_attributes, history_attributes):
                    attributes.assert_any_call("-topmost", enabled)
                # Xvfb without a window manager accepts the request but does
                # not report a changed topmost property. Windows applies it.
                if self.root.tk.call("tk", "windowingsystem") == "win32":
                    for window in windows:
                        self.assertEqual(bool(window.attributes("-topmost")), enabled)
                saved = json.loads((self.directory / "settings.json").read_text(encoding="utf-8"))
                self.assertIs(saved["always_on_top"], enabled)

    def test_completion_sound_plays_once_for_a_batch_and_respects_disabled_setting(self):
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app.folder_mode.set("custom")
        self.app.folder.set(str(self.directory / "output"))
        self.app._format_changed()
        sources = [self.image(f"sound-{index}.png") for index in range(2)]
        self.app.completion_sound.set(True)
        with patch("compressor.ui.play_completion_sound") as sound:
            self.app.start(sources)
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(len(self.app.results), 2)
            self.assertEqual(self.app.failures, [])
            sound.assert_called_once_with(self.root)
            self.app.completion_sound.set(False)
            self.app.start(sources)
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(len(self.app.results), 2)
            sound.assert_called_once_with(self.root)

    def test_successful_batch_can_close_automatically_after_notifying(self):
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app.folder_mode.set("custom")
        self.app.folder.set(str(self.directory / "output"))
        self.app._format_changed()
        self.app.completion_sound.set(True)
        self.app.auto_close.set(True)
        events = []
        with patch("compressor.ui.play_completion_sound", side_effect=lambda _root: events.append("sound")) as sound, \
                patch.object(self.app, "_destroy", side_effect=lambda: events.append("close")) as destroy:
            self.app.start([self.image("automatic-close.png")])
            self.wait_for(lambda: destroy.called)
            self.assertFalse(self.app.busy)
            self.assertTrue(self.app.closing)
            self.assertEqual(len(self.app.results), 1)
            self.assertEqual(self.app.failures, [])
            sound.assert_called_once_with(self.root)
            destroy.assert_called_once_with()
            self.root.update()
            self.assertEqual(events, ["sound", "close"])

    def test_failed_batch_notifies_but_remains_open_for_error_details(self):
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app.folder_mode.set("custom")
        self.app.folder.set(str(self.directory / "output"))
        self.app._format_changed()
        corrupt = self.directory / "broken.png"
        corrupt.write_bytes(b"not an image")
        self.app.completion_sound.set(True)
        self.app.auto_close.set(True)
        with patch("compressor.ui.play_completion_sound") as sound, patch.object(self.app, "_destroy") as destroy:
            self.app.start([self.image("successful.png"), corrupt])
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(len(self.app.results), 1)
            self.assertEqual(len(self.app.failures), 1)
            self.assertTrue(self.app.history_visible)
            self.assertFalse(self.app.closing)
            sound.assert_called_once_with(self.root)
            destroy.assert_not_called()

    def test_batch_without_supported_images_neither_notifies_nor_closes(self):
        self.app.completion_sound.set(True)
        self.app.auto_close.set(True)
        with patch("compressor.ui.play_completion_sound") as sound, patch.object(self.app, "_destroy") as destroy:
            self.app.start([self.directory / "missing.png"])
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(self.app.results, [])
            self.assertEqual(self.app.failures, [])
            self.assertFalse(self.app.closing)
            sound.assert_not_called()
            destroy.assert_not_called()

    def test_closing_during_conversion_suppresses_completion_sound(self):
        entered = threading.Event()
        release = threading.Event()
        done_queued = threading.Event()
        self.app.completion_sound.set(True)
        self.app.auto_close.set(True)

        def slow_convert(source, settings, output_dir):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test encoder release timed out")
            return convert_image(source, settings, output_dir)

        put_event = self.app.events.put

        def record_event(event):
            put_event(event)
            if event[0] == "done":
                done_queued.set()

        with patch("compressor.ui.convert_image", side_effect=slow_convert), \
                patch("compressor.ui.play_completion_sound") as sound, \
                patch.object(self.app.events, "put", side_effect=record_event), \
                patch.object(self.app, "_destroy") as destroy:
            self.app.start([self.image("closing.png")])
            try:
                self.wait_for(entered.is_set)
                release.set()
                # Close after the worker queues a normal completion but
                # before Tk consumes it. This also covers the close/finish
                # race where the queued cancelled flag is still False.
                self.assertTrue(done_queued.wait(timeout=5))
                self.app.close()
                self.assertTrue(self.app.closing)
                self.assertTrue(self.app.cancel_event.is_set())
            finally:
                release.set()
            self.wait_for(lambda: destroy.called)
            self.assertFalse(self.app.busy)
            sound.assert_not_called()
            destroy.assert_called_once_with()

    def test_cancel_after_completion_is_queued_suppresses_sound_and_automatic_close(self):
        entered = threading.Event()
        release = threading.Event()
        done_queued = threading.Event()
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app.folder_mode.set("custom")
        self.app.folder.set(str(self.directory / "output"))
        self.app._format_changed()
        self.app.completion_sound.set(True)
        self.app.auto_close.set(True)

        def slow_convert(source, settings, output_dir):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test encoder release timed out")
            return convert_image(source, settings, output_dir)

        put_event = self.app.events.put

        def record_event(event):
            put_event(event)
            if event[0] == "done":
                self.assertIs(event[1], False)
                done_queued.set()

        with patch("compressor.ui.convert_image", side_effect=slow_convert), \
                patch("compressor.ui.play_completion_sound") as sound, \
                patch.object(self.app.events, "put", side_effect=record_event), \
                patch.object(self.app, "_destroy") as destroy:
            self.app.start([self.image("queued-completion.png")])
            try:
                self.wait_for(entered.is_set)
                release.set()
                self.assertTrue(done_queued.wait(timeout=5))
                self.app.cancel()
                self.assertTrue(self.app.cancel_event.is_set())
            finally:
                release.set()
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(len(self.app.results), 1)
            self.assertEqual(self.app.failures, [])
            self.assertIn("中止", self.app.status.get())
            self.assertFalse(self.app.closing)
            sound.assert_not_called()
            destroy.assert_not_called()

    def test_mixed_formats_replace_originals_only_when_checked(self):
        self.app.output_format.set(FORMAT_LABELS["PNG"])
        self.app._format_changed()
        sources = []
        for name in ("mixed.bmp", "mixed.tif"):
            path = self.directory / name
            with Image.new("RGB", (64, 32), (30, 90, 150)) as image:
                image.save(path)
            sources.append(path)
        originals = {source: source.read_bytes() for source in sources}
        self.app.folder_mode.set("custom")
        custom = self.directory / "separate"
        self.app.folder.set(str(custom))
        self.app.start(sources)
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.app.failures, [])
        for source in sources:
            self.assertEqual(source.read_bytes(), originals[source])
        self.assertEqual(list(custom.glob("*.png")), [custom / "mixed.png"])

        self.app.overwrite_check.invoke()
        self.app.start(sources)
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.app.failures, [])
        self.assertEqual(len(self.app.results), 2)
        for source in sources:
            self.assertFalse(source.exists())
        self.assertEqual({result.output_path for result in self.app.results}, {self.directory / "mixed.png"})
        with Image.open(self.directory / "mixed.png") as image:
            self.assertEqual(image.size, (64, 32))
            self.assertEqual(image.getpixel((0, 0)), (30, 90, 150))
        self.assertEqual(list(custom.glob("*.png")), [custom / "mixed.png"])
        self.assertFalse(list(self.directory.glob(".image-compressor-*")))
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (196, 328))

    def test_heif_selection_preserves_alpha_and_overwrites_previous_output(self):
        if "HEIF" not in self.app.formats:
            self.skipTest("HEIF encoder is not installed")
        self.app.output_format.set(FORMAT_LABELS["HEIF"])
        self.app._format_changed()
        self.assertFalse(self.app.speed_combo.instate(["disabled"]))
        source = self.image("写真.png")
        output = self.directory / "写真.heif"
        for color in ((120, 60, 200, 128), (15, 200, 80, 64)):
            with Image.new("RGBA", (64, 32), color) as image:
                image.save(source)
            original = source.read_bytes()
            self.app.start([source])
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(self.app.failures, [])
            self.assertEqual(self.app.results[0].output_path, output)
            self.assertEqual(source.read_bytes(), original)
            with Image.open(output) as image:
                self.assertEqual(image.convert("RGBA").getpixel((0, 0)), color)
        self.assertEqual(list(self.directory.glob("*.heif")), [output])

    def test_main_controls_fit_their_requested_size(self):
        controls = (
            self.app.same_radio, self.app.custom_radio,
            self.app.format_combo, self.app.preset_combo, self.app.size_combo,
            self.app.browse_button, self.app.advanced_button, self.app.folder_button,
            self.app.history_button, self.app.open_button, self.app.cancel_button,
            self.app.overwrite_check,
        )
        for widget in controls:
            with self.subTest(widget=str(widget)):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth())
                self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())

    def test_window_position_is_saved_and_restored_with_settings(self):
        self.root.geometry("+300+250")
        self.root.update()
        original = self.app.window_position.capture()
        self.app._save_settings()
        saved = json.loads((self.directory / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["window_position"], original.to_settings())
        self.root.geometry("+50+50")
        self.root.update()
        self.app._load_settings()
        self.app.window_position.restore()
        self.root.update()
        self.assertEqual(self.app.window_position.capture(), original)
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), COMPACT_CLIENT_SIZE)


if __name__ == "__main__":
    unittest.main()
