"""Compact Tk desktop interface; all image work runs off the UI thread."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
import unicodedata
from tkinter import filedialog, font, messagebox, ttk

from .window_chrome import dark_native_caption, install_window_chrome
from .window_state import WindowPositionTracker

from .engine import (
    INPUT_EXTENSIONS,
    CompressionResult,
    CompressionSettings,
    available_formats,
    collect_images,
    convert_image,
)


FORMAT_LABELS = {
    "AVIF": "AVIF（おすすめ）",
    "HEIF": "HEIF / HEIC",
    "JPEG": "JPEG",
    "WEBP": "WebP",
    "PNG": "PNG（可逆圧縮）",
}
PRESETS = {"標準": 65, "高画質": 85, "サイズ優先": 40}
PRESERVE_PRESET = "完全保持（可逆）"
SPEEDS = {"高速": 8, "標準": 6, "高圧縮": 4}
SIZES = {
    "100%（変更なし）": 100,
    "90%": 90,
    "80%": 80,
    "75%": 75,
    "50%": 50,
    "25%": 25,
    "10%": 10,
}
BACKGROUND = "#1e1e1e"
PANEL = "#252526"
CAPTION = "#181818"
FIELD = "#313131"
TEXT = "#cccccc"
MUTED = "#9d9d9d"
ACCENT = "#007acc"
COMPACT_CLIENT_SIZE = (196, 328)
CAPTION_HEIGHT = 24


def settings_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ImageCompressor" / "settings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "image-compressor" / "settings.json"


def readable_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


class CompressorApp:
    def __init__(self, root: tk.Tk, dnd_available: bool = False):
        self.root = root
        # Idle updates used by fonts, native chrome and DnD must not map the
        # default Tk window before its content, size and position are ready.
        self.root.withdraw()
        self.dnd_available = dnd_available
        self.root.title("圧縮")
        self.root.configure(background=BACKGROUND)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.closing = False
        self.results: list[CompressionResult] = []
        self.failures: list[tuple[Path, str]] = []
        self.last_output: Path | None = None
        self.last_input: Path | None = None
        self.history_visible = False
        self.controls: list[ttk.Widget] = []
        self.formats = available_formats()
        if not self.formats:
            raise RuntimeError("画像のエンコーダーが見つかりません。requirements.txtをインストールしてください。")
        self._setup_style()
        self._setup_icon()
        self.chrome = install_window_chrome(self.root)
        self.output_format = tk.StringVar(value=FORMAT_LABELS[self.formats[0]])
        self.preset = tk.StringVar(value="標準")
        self.quality = tk.DoubleVar(value=65)
        self.quality_text = tk.StringVar(value="65")
        self.speed = tk.StringVar(value="標準")
        self.max_size = tk.StringVar(value="100%（変更なし）")
        self.keep_metadata = tk.BooleanVar(value=False)
        self.preserve_image = tk.BooleanVar(value=True)
        self.overwrite_original = tk.BooleanVar(value=False)
        self.preservation_note = tk.StringVar(value="全画素を保持")
        self.folder_mode = tk.StringVar(value="same")
        self.folder = tk.StringVar(value=str(Path.home() / "Pictures"))
        self.status = tk.StringVar(value="待機中")
        self.summary = tk.StringVar(value="画像を選ぶと圧縮を開始します")
        self.format_note = tk.StringVar()
        self.window_position = WindowPositionTracker(self.root)
        self._load_settings()
        self._build_ui()
        self._refresh_controls()
        self._update_format_note()
        if dnd_available:
            self._bind_drop_targets(self.main)
        self.root.bind("<Control-o>", lambda _event: self.choose_files())
        self.root.bind("<Control-d>", lambda _event: self.show_advanced())
        self.root.bind("<Control-r>", lambda _event: self.toggle_history())
        self.root.bind("<Escape>", lambda _event: self.cancel())
        self._poll_id = self.root.after(80, self._poll_events)
        self.root.update_idletasks()
        width, height = COMPACT_CLIENT_SIZE
        self.minimum_height = height
        self.root.minsize(width, self.minimum_height)
        self.root.maxsize(width, self.minimum_height)
        self.root.geometry(f"{width}x{self.minimum_height}")
        self.chrome.apply()
        self.window_position.restore()
        self.root.deiconify()
        self.chrome.apply()
        self.window_position.capture()

    def _setup_icon(self):
        assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "assets"
        self.icon_images = []
        try:
            from PIL import Image, ImageTk

            with Image.open(assets / "app-icon.png") as original:
                self.icon_images = [
                    ImageTk.PhotoImage(original.resize((size, size), Image.Resampling.LANCZOS), master=self.root)
                    for size in (16, 32, 48, 64, 128, 256)
                ]
            self.root.iconphoto(True, *self.icon_images)
            if sys.platform == "win32":
                self.root.iconbitmap(default=str(assets / "app-icon.ico"))
                self.root.iconbitmap(str(assets / "app-icon.ico"))
        except (OSError, tk.TclError):
            # A source checkout without the optional artwork still opens.
            pass

    def _setup_style(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        families = set(font.families(self.root))
        family = font.nametofont("TkDefaultFont").actual("family")
        for name in ("Yu Gothic UI", "Meiryo", "Noto Sans CJK JP", "Noto Sans JP", "IPAGothic"):
            if name in families:
                family = name
                break
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
            font.nametofont(name).configure(family=family, size=-11)
        self.font_family = family
        style.configure(
            ".", font=(family, -11), background=BACKGROUND, foreground=TEXT,
            fieldbackground=FIELD, troughcolor=CAPTION, bordercolor=FIELD,
            lightcolor=FIELD, darkcolor=FIELD, selectbackground=ACCENT,
            selectforeground="#ffffff",
        )
        for name in ("TButton", "Compact.TButton"):
            style.configure(
                name, font=(family, -11), background=FIELD, foreground=TEXT,
                borderwidth=0, relief="flat", padding=(6, 3), width=0,
            )
            style.map(
                name, background=[("disabled", PANEL), ("pressed", "#474747"), ("active", "#3c3c3c")],
                foreground=[("disabled", "#666666")],
            )
        style.configure("Compact.TButton", padding=(3, 1))
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", borderwidth=0, width=0)
        style.configure("Accent.TButton", padding=(4, 1))
        style.map("Accent.TButton", background=[("disabled", FIELD), ("pressed", "#0062a3"), ("active", "#168cda")])
        caption_family = "Segoe UI" if "Segoe UI" in families else family
        for name in ("Caption.TButton", "CloseCaption.TButton"):
            style.configure(
                name, background=CAPTION, foreground=TEXT, borderwidth=0,
                relief="flat", font=(caption_family, -18), padding=0, width=0,
            )
            style.map(
                name, background=[("pressed", "#3c3c3c"), ("active", "#303030")],
            )
        style.map("CloseCaption.TButton", background=[("pressed", "#a82218"), ("active", "#c42b1c")], foreground=[("active", "white")])
        style.configure("TCombobox", arrowsize=10, padding=(4, 1), borderwidth=0, arrowcolor=TEXT)
        style.map(
            "TCombobox", fieldbackground=[("disabled", PANEL), ("readonly", FIELD)],
            foreground=[("disabled", "#777777"), ("readonly", TEXT)],
            selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
            background=[("active", "#3c3c3c"), ("readonly", FIELD)],
        )
        style.configure("TEntry", padding=(4, 1), borderwidth=0, insertcolor=TEXT)
        style.map("TEntry", fieldbackground=[("disabled", "#202020")], foreground=[("disabled", "#777777")])
        style.configure("Panel.TCheckbutton", background=PANEL, foreground=TEXT, font=(family, -11), padding=0, indicatorbackground=FIELD)
        style.map("Panel.TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", "#777777")], indicatorbackground=[("selected", ACCENT), ("active", "#3c3c3c")])
        self._setup_radio_style(style)
        style.configure("Horizontal.TScale", background=ACCENT, troughcolor=FIELD, borderwidth=0, bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD)
        style.map("Horizontal.TScale", background=[("disabled", "#777777"), ("active", "#168cda")])
        style.configure("TProgressbar", thickness=3, background=ACCENT, troughcolor=CAPTION, borderwidth=0, bordercolor=CAPTION, lightcolor=CAPTION, darkcolor=CAPTION)
        style.configure("Treeview", rowheight=25, background=BACKGROUND, fieldbackground=BACKGROUND, foreground=TEXT, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL, foreground=TEXT, relief="flat", padding=5)
        style.map("Treeview", background=[("selected", "#094771")], foreground=[("selected", "#ffffff")])
        style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BACKGROUND, borderwidth=0, arrowcolor=MUTED)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.font", (family, -11))

    def _setup_radio_style(self, style: ttk.Style):
        from PIL import Image, ImageDraw, ImageTk

        # Replace only the indicator; ttk keeps focus, keyboard navigation and
        # the full text row as the clickable area. Images stay owned by the app.
        self.radio_images = {}
        for state, outline, dot in (
            ("normal", "#797979", None),
            ("active", "#b0b0b0", None),
            ("selected", "#3794ff", "#3794ff"),
            ("selected_active", "#75beff", "#75beff"),
            ("disabled", "#515151", None),
            ("selected_disabled", "#626262", "#777777"),
        ):
            with Image.new("RGBA", (64, 48)) as indicator:
                drawing = ImageDraw.Draw(indicator)
                drawing.ellipse((4, 4, 44, 44), fill=FIELD, outline=outline, width=4)
                if dot:
                    drawing.ellipse((16, 16, 32, 32), fill=dot)
                with indicator.resize((16, 12), Image.Resampling.LANCZOS) as resized:
                    self.radio_images[state] = ImageTk.PhotoImage(resized, master=self.root)
        style.element_create(
            "ModernRadio.indicator", "image", self.radio_images["normal"],
            (("disabled", "selected"), self.radio_images["selected_disabled"]),
            ("disabled", self.radio_images["disabled"]),
            (("active", "selected"), self.radio_images["selected_active"]),
            ("selected", self.radio_images["selected"]),
            ("active", self.radio_images["active"]),
        )
        layout = style.layout("TRadiobutton")

        def replace_indicator(elements):
            for index, (name, options) in enumerate(elements):
                if name.endswith(".indicator"):
                    elements[index] = ("ModernRadio.indicator", options)
                if "children" in options:
                    replace_indicator(options["children"])

        replace_indicator(layout)
        style.layout("Panel.TRadiobutton", layout)
        style.configure("Panel.TRadiobutton", background=PANEL, foreground=TEXT, font=(self.font_family, -11), padding=0)
        style.map(
            "Panel.TRadiobutton", background=[("disabled", PANEL), ("active", "#2a2d2e")],
            foreground=[("disabled", "#777777")],
        )

    def _panel(self, title: str, y: int, height: int) -> tk.Frame:
        panel = tk.Frame(self.main, bg=PANEL, bd=0, highlightthickness=0)
        panel.place(x=6, y=y, width=184, height=height)
        tk.Label(panel, text=title, bg=PANEL, fg=TEXT, padx=0, pady=0, anchor="w", font=(self.font_family, -11, "bold")).place(
            x=6, y=4, width=172, height=19,
        )
        return panel

    def _label(self, parent, text, **kwargs):
        return tk.Label(parent, text=text, bg=PANEL, fg=TEXT, anchor="w", padx=0, pady=0, **kwargs)

    def _build_titlebar(self):
        if not self.chrome.enabled:
            return
        self.titlebar = tk.Frame(self.root, bg=CAPTION, height=CAPTION_HEIGHT, bd=0, highlightthickness=0)
        self.titlebar.pack(fill="x")
        self.titlebar.pack_propagate(False)
        self.title_icon = tk.Label(self.titlebar, bg=CAPTION, bd=0, padx=0, pady=0)
        if self.icon_images:
            self.title_icon.configure(image=self.icon_images[0])
        self.title_icon.place(x=6, y=4, width=16, height=16)
        self.title_label = tk.Label(self.titlebar, text="画像圧縮", bg=CAPTION, fg=TEXT, anchor="w", padx=0, pady=0)
        self.title_label.place(x=26, y=0, width=118, height=CAPTION_HEIGHT)
        for widget in (self.titlebar, self.title_icon, self.title_label):
            widget.bind("<ButtonPress-1>", self.chrome.start_drag)
        self.minimize_button = ttk.Button(self.titlebar, text="−", command=self.chrome.minimize, style="Caption.TButton", takefocus=False)
        self.minimize_button.place(x=148, y=0, width=24, height=CAPTION_HEIGHT)
        self.close_button = ttk.Button(self.titlebar, text="×", command=self.close, style="CloseCaption.TButton", takefocus=False)
        self.close_button.place(x=172, y=0, width=24, height=CAPTION_HEIGHT)

    def _build_ui(self):
        self._build_titlebar()
        self.main = tk.Frame(self.root, bg=BACKGROUND, bd=0, highlightthickness=0)
        self.main.pack(fill="both", expand=True)
        output_row = tk.Frame(self.main, bg=BACKGROUND)
        output_row.place(x=6, y=2, width=184, height=23)
        tk.Label(output_row, text="出力", bg=BACKGROUND, fg=MUTED, padx=0, pady=0, anchor="w").place(x=0, y=0, width=32, height=23)
        self.format_combo = ttk.Combobox(
            output_row, textvariable=self.output_format,
            values=[FORMAT_LABELS[f] for f in self.formats], state="readonly", width=12,
        )
        self.format_combo.place(x=36, y=0, width=148, height=23)
        self.format_combo.bind("<<ComboboxSelected>>", self._format_changed)
        self.controls.append(self.format_combo)

        options = self._panel("出力設定", 28, 91)
        self._label(options, "画質").place(x=6, y=24, width=32, height=22)
        self.preset_combo = ttk.Combobox(
            options, textvariable=self.preset,
            values=[*PRESETS, PRESERVE_PRESET, "カスタム"], state="readonly", width=10,
        )
        self.preset_combo.place(x=42, y=23, width=136, height=23)
        self.preset_combo.bind("<<ComboboxSelected>>", self._preset_changed)
        self.controls.append(self.preset_combo)
        quality_row = tk.Frame(options, bg=PANEL)
        quality_row.place(x=6, y=46, width=172, height=19)
        self.quality_scale = ttk.Scale(quality_row, from_=1, to=100, variable=self.quality, command=self._quality_changed)
        self.quality_scale.place(x=0, y=2, width=140, height=14)
        self.quality_value = self._label(quality_row, "")
        self.quality_value.configure(textvariable=self.quality_text, anchor="e", fg=MUTED)
        self.quality_value.place(x=144, y=0, width=28, height=19)
        self.controls.append(self.quality_scale)
        self._label(options, "サイズ").place(x=6, y=67, width=34, height=22)
        self.size_combo = ttk.Combobox(options, textvariable=self.max_size, values=list(SIZES), state="readonly", width=10)
        self.size_combo.place(x=42, y=66, width=136, height=23)
        self.size_combo.bind("<FocusOut>", self._size_changed)
        self.size_combo.bind("<Return>", self._size_changed)
        self.controls.append(self.size_combo)
        self.advanced_button = ttk.Button(options, text="詳細…", command=self.show_advanced, style="Compact.TButton")
        self.advanced_button.place(x=130, y=2, width=48, height=21)

        destination = self._panel("出力フォルダー", 122, 87)
        self.same_radio = ttk.Radiobutton(destination, text="入力と同じフォルダ", variable=self.folder_mode, value="same", command=self._refresh_controls, style="Panel.TRadiobutton")
        self.custom_radio = ttk.Radiobutton(destination, text="指定したフォルダ", variable=self.folder_mode, value="custom", command=self._refresh_controls, style="Panel.TRadiobutton")
        self.same_radio.place(x=6, y=23, width=172, height=19)
        self.custom_radio.place(x=6, y=42, width=172, height=19)
        self.controls.extend((self.same_radio, self.custom_radio))
        self.folder_entry = ttk.Entry(destination, textvariable=self.folder, width=12)
        self.folder_entry.place(x=6, y=62, width=145, height=23)
        self.folder_button = ttk.Button(destination, text="…", command=self.choose_folder, style="Compact.TButton")
        self.folder_button.place(x=154, y=62, width=24, height=23)

        self.drop_panel = tk.Frame(self.main, bg=PANEL, bd=0, highlightthickness=0, cursor="hand2")
        self.drop_panel.place(x=6, y=212, width=184, height=44)
        self.drop_icon = tk.Canvas(self.drop_panel, bg=PANEL, width=19, height=23, highlightthickness=0)
        self.drop_icon.place(x=6, y=11, width=19, height=23)
        self.drop_icon.create_line(2, 1, 11, 1, 16, 6, 16, 21, 2, 21, 2, 1, fill=MUTED, width=1)
        self.drop_icon.create_line(11, 1, 11, 6, 16, 6, fill=MUTED, width=1)
        self.drop_icon.create_line(9, 9, 9, 18, fill="#3794ff", width=1)
        self.drop_icon.create_line(6, 15, 9, 18, 12, 15, fill="#3794ff", width=1)
        self.drop_title = tk.Label(self.drop_panel, text="ここに画像をドロップ" if self.dnd_available else "画像を選んで圧縮", bg=PANEL, fg=TEXT, anchor="w", padx=0, pady=0)
        self.drop_title.place(x=32, y=2, width=146, height=19)
        self.browse_button = ttk.Button(self.drop_panel, text="画像を選ぶ…", command=self.choose_files, style="Accent.TButton")
        self.browse_button.place(x=32, y=22, width=104, height=21)
        self.drop_hint = tk.Label(self.drop_panel, text="複数可", bg=PANEL, fg=MUTED, padx=0, pady=0)
        self.drop_hint.place(x=140, y=23, width=38, height=19)
        for widget in (self.drop_panel, self.drop_icon, self.drop_title, self.drop_hint):
            widget.bind("<Button-1>", lambda _event: self.choose_files())

        self.progress = ttk.Progressbar(self.main, mode="determinate", maximum=1)
        self.progress.place(x=6, y=259, width=184, height=2)
        self.status_label = tk.Label(self.main, textvariable=self.status, bg=BACKGROUND, fg=MUTED, anchor="w", padx=0, pady=0)
        self.status_label.place(x=6, y=261, width=184, height=19)
        self.history_button = ttk.Button(self.main, text="結果", command=self.toggle_history, state="disabled", style="Compact.TButton")
        self.history_button.place(x=6, y=281, width=42, height=21)
        self.open_button = ttk.Button(self.main, text="保存先", command=self.open_folder, state="disabled", style="Compact.TButton")
        self.open_button.place(x=52, y=281, width=52, height=21)
        self.cancel_button = ttk.Button(self.main, text="中止", command=self.cancel, state="disabled", style="Compact.TButton")
        self.cancel_button.place(x=148, y=281, width=42, height=21)
        self._build_advanced_window()
        self._build_history_window()

    def _build_advanced_window(self):
        self.advanced_window = tk.Toplevel(self.root)
        self.advanced_window.withdraw()
        self.advanced_window.title("詳細設定")
        self.advanced_window.configure(bg=PANEL)
        self.advanced_window.transient(self.root)
        self.advanced_window.resizable(False, False)
        self.advanced_window.protocol("WM_DELETE_WINDOW", self.advanced_window.withdraw)
        self.advanced_window.bind("<Escape>", lambda _event: self.advanced_window.withdraw())
        body = tk.Frame(self.advanced_window, bg=PANEL, padx=8, pady=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        self.preservation_check = ttk.Checkbutton(
            body, text="全画素を完全保持（可逆）", variable=self.preserve_image,
            command=self._preservation_changed, style="Panel.TCheckbutton",
        )
        self.preservation_check.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.controls.append(self.preservation_check)
        self._label(body, "圧縮速度").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.speed_combo = ttk.Combobox(body, textvariable=self.speed, values=list(SPEEDS), state="readonly", width=12)
        self.speed_combo.grid(row=1, column=1, sticky="ew")
        self.controls.append(self.speed_combo)
        self.metadata_check = ttk.Checkbutton(
            body, text="撮影情報（EXIF）を残す", variable=self.keep_metadata, style="Panel.TCheckbutton",
        )
        self.metadata_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(9, 0))
        self.controls.append(self.metadata_check)
        self.overwrite_check = ttk.Checkbutton(
            body, text="元のファイルを上書き", variable=self.overwrite_original,
            command=self._overwrite_changed, style="Panel.TCheckbutton",
        )
        self.overwrite_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(9, 0))
        self.controls.append(self.overwrite_check)
        tk.Label(
            body, text="有効時は元のフォルダーに保存します。\n形式が変わる場合は拡張子も変え、\n保存・検証後に元ファイルを削除します。",
            bg=PANEL, fg=MUTED, anchor="w", justify="left", wraplength=260,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(body, textvariable=self.format_note, bg=PANEL, fg=MUTED, anchor="w", justify="left", wraplength=260).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(7, 0),
        )
        ttk.Button(body, text="閉じる", command=self.advanced_window.withdraw).grid(row=6, column=1, sticky="e", pady=(10, 0))
        dark_native_caption(self.advanced_window)

    def _build_history_window(self):
        self.history_window = tk.Toplevel(self.root)
        self.history_window.withdraw()
        self.history_window.title("圧縮結果")
        self.history_window.configure(bg=BACKGROUND)
        self.history_window.transient(self.root)
        self.history_window.geometry("420x220")
        self.history_window.minsize(300, 170)
        self.history_window.protocol("WM_DELETE_WINDOW", self._hide_history)
        self.history_window.bind("<Escape>", lambda _event: self._hide_history())
        self.history_panel = tk.Frame(self.history_window, bg=BACKGROUND, padx=7, pady=7)
        self.history_panel.pack(fill="both", expand=True)
        self.summary_label = tk.Label(
            self.history_panel, textvariable=self.summary, bg=BACKGROUND, fg=TEXT,
            anchor="w", justify="left", wraplength=390, height=2,
        )
        self.summary_label.pack(fill="x", pady=(0, 5))
        table = tk.Frame(self.history_panel, bg=BACKGROUND)
        table.pack(fill="both", expand=True)
        self.history = ttk.Treeview(table, columns=("name", "result"), show="headings", height=5)
        self.history.heading("name", text="ファイル（ダブルクリックで詳細）")
        self.history.heading("result", text="結果")
        self.history.column("name", width=260, minwidth=130)
        self.history.column("result", width=100, minwidth=80, stretch=False)
        self.history.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.history.yview)
        scrollbar.pack(side="right", fill="y")
        self.history.configure(yscrollcommand=scrollbar.set)
        self.history.bind("<Double-1>", self.show_result_details)
        self.history.bind("<Return>", self.show_result_details)
        self.history_details: dict[str, str] = {}
        dark_native_caption(self.history_window)

    def show_advanced(self):
        self.advanced_window.deiconify()
        dark_native_caption(self.advanced_window)
        self.advanced_window.lift()
        self.advanced_window.focus_set()

    def _bind_drop_targets(self, widget):
        # TkDnD sends events to the widget directly underneath the pointer.
        if widget not in (self.folder_entry, self.history):
            widget.drop_target_register("DND_Files")
            widget.dnd_bind("<<Drop>>", self._drop)
        for child in widget.winfo_children():
            self._bind_drop_targets(child)

    def _selected_format(self) -> str:
        return next(key for key, value in FORMAT_LABELS.items() if value == self.output_format.get())

    def _format_changed(self, _event=None):
        self._refresh_controls()
        self._update_format_note()

    def _preservation_changed(self):
        self._refresh_controls()
        self._update_format_note()

    def _overwrite_changed(self):
        self._refresh_controls()

    def _selected_scale_percent(self) -> int:
        value = self.max_size.get()
        if value in SIZES:
            return SIZES[value]
        value = unicodedata.normalize("NFKC", value).strip()
        if value.endswith("%"):
            value = value[:-1].strip()
        if value.isascii() and value.isdecimal() and 1 <= int(value) <= 100:
            return int(value)
        raise ValueError("画像サイズは 1〜100% の整数で指定してください。")

    def _size_changed(self, _event=None):
        try:
            percent = self._selected_scale_percent()
        except ValueError:
            return
        self.max_size.set("100%（変更なし）" if percent == 100 else f"{percent}%")

    def _update_format_note(self):
        output = self._selected_format()
        if self.preserve_image.get():
            self.format_note.set("元の画素・透明度・色を保持します。\n画像サイズも変更しません。\n容量が増える場合があります。")
            return
        notes = {
            "AVIF": "対応GPUを自動使用（色の間引きあり）。\n非対応時はCPUで色の間引きなし。\n細部は品質設定に応じて変わります。",
            "HEIF": "対応GPUでHEVC圧縮を自動使用します。\n透明度付きは可逆で保存します。\n細部は品質設定に応じて変わります。",
            "JPEG": "非可逆圧縮です。透明部分は白になります。",
            "WEBP": "非可逆圧縮です。\n細部は品質設定に応じて変わります。",
            "PNG": "画質を変えずに圧縮します",
        }
        self.format_note.set(notes[output])

    def _preset_changed(self, _event=None):
        preset = self.preset.get()
        self.preserve_image.set(preset == PRESERVE_PRESET)
        value = PRESETS.get(preset)
        if value is not None:
            self.quality.set(value)
        self._refresh_controls()
        self._update_format_note()

    def _quality_changed(self, value):
        if self.preserve_image.get():
            self.quality_text.set("—")
            return
        quality = max(1, min(100, round(float(value))))
        self.quality_text.set(str(quality))
        preset = next((name for name, number in PRESETS.items() if number == quality), "カスタム")
        self.preset.set(preset)

    def _refresh_controls(self):
        preserving = self.preserve_image.get()
        visible_formats = tuple(f for f in self.formats if not preserving or f != "JPEG")
        self.format_combo.configure(values=[FORMAT_LABELS[f] for f in visible_formats])
        if preserving:
            if self._selected_format() == "JPEG":
                self.output_format.set(FORMAT_LABELS[visible_formats[0]])
            self.preset.set(PRESERVE_PRESET)
            self.quality_text.set("—")
            self.max_size.set("100%（変更なし）")
            self.preservation_note.set("全画素を保持")
        else:
            quality = round(self.quality.get())
            self.quality_text.set(str(quality))
            self.preset.set(next((name for name, value in PRESETS.items() if value == quality), "カスタム"))
            self.preservation_note.set({
                "PNG": "可逆圧縮", "AVIF": "GPU / CPU 自動", "HEIF": "GPU / CPU 自動",
            }.get(self._selected_format(), "非可逆圧縮"))
        for control in self.controls:
            state = "disabled" if self.busy else "readonly" if isinstance(control, ttk.Combobox) else "normal"
            control.configure(state=state)
        if not self.busy:
            if self._selected_format() == "PNG":
                self.preset_combo.configure(state="disabled")
            if preserving or self._selected_format() == "PNG":
                self.quality_scale.configure(state="disabled")
            if preserving:
                self.size_combo.configure(state="disabled")
            else:
                self.size_combo.configure(state="normal")
            if self._selected_format() not in {"AVIF", "HEIF"}:
                self.speed_combo.configure(state="disabled")
        replacing = self.overwrite_original.get()
        if replacing:
            self.folder_mode.set("same")
            self.same_radio.configure(text="元ファイルを置き換え", state="disabled")
            self.custom_radio.configure(state="disabled")
        else:
            self.same_radio.configure(text="入力と同じフォルダ")
        custom_enabled = not self.busy and not replacing and self.folder_mode.get() == "custom"
        self.folder_entry.configure(state="normal" if custom_enabled else "disabled")
        self.folder_button.configure(state="normal" if custom_enabled else "disabled")
        self.browse_button.configure(state="disabled" if self.busy else "normal")
        self.cancel_button.configure(state="normal" if self.busy and not self.cancel_event.is_set() else "disabled")

    def choose_folder(self):
        selected = filedialog.askdirectory(parent=self.root, title="出力フォルダーを選択", initialdir=self.folder.get() or str(Path.home()))
        if selected:
            self.folder.set(selected)

    def choose_files(self):
        if self.busy:
            return
        selected = filedialog.askopenfilenames(
            parent=self.root, title="圧縮する画像を選択",
            initialdir=str(self.last_input or Path.home() / "Pictures"),
            filetypes=[("画像ファイル", " ".join(f"*{ext}" for ext in sorted(INPUT_EXTENSIONS))), ("すべてのファイル", "*")],
        )
        if selected:
            self.start([Path(value) for value in selected])

    def _drop(self, event):
        if self.busy:
            return "refuse_drop"
        try:
            paths = [Path(value) for value in self.root.tk.splitlist(event.data)]
        except tk.TclError:
            self.status.set("ドロップされたファイルを読み取れませんでした")
            return "refuse_drop"
        self.start(paths)
        return "copy"

    def start(self, paths: list[Path]):
        if self.busy or not paths:
            return
        output_dir = None
        if not self.overwrite_original.get() and self.folder_mode.get() == "custom":
            raw_folder = self.folder.get().strip()
            if not raw_folder:
                messagebox.showerror("出力フォルダー", "保存先のフォルダーを指定してください。", parent=self.root)
                return
            output_dir = Path(raw_folder).expanduser()
            if output_dir.exists() and not output_dir.is_dir():
                messagebox.showerror("出力フォルダー", "指定した保存先はフォルダーではありません。", parent=self.root)
                return
        try:
            settings = CompressionSettings(
                output_format=self._selected_format(), quality=round(self.quality.get()),
                speed=SPEEDS[self.speed.get()], scale_percent=self._selected_scale_percent(),
                preserve_metadata=self.keep_metadata.get(),
                preserve_image=self.preserve_image.get(),
                overwrite_original=self.overwrite_original.get(),
            )
        except ValueError as error:
            messagebox.showerror("圧縮設定", str(error), parent=self.root)
            return
        self.busy = True
        self.cancel_event.clear()
        self.results.clear()
        self.failures.clear()
        self.last_output = None
        self.open_button.configure(state="disabled")
        for item in self.history.get_children():
            self.history.delete(item)
        self.history_details.clear()
        self.history_button.configure(state="disabled")
        self.status.set("画像を確認中…")
        self.summary.set("フォルダー内の画像もまとめて処理します")
        self.drop_title.configure(text="圧縮しています…")
        self.progress.configure(value=0, maximum=1)
        self._refresh_controls()
        self._save_settings()
        threading.Thread(target=self._worker, args=(paths, settings, output_dir), daemon=True).start()

    def _worker(self, paths, settings, output_dir):
        try:
            files = collect_images(paths)
            self.events.put(("discovered", len(files)))
            for index, path in enumerate(files, 1):
                if self.cancel_event.is_set():
                    break
                self.events.put(("start", (path, index, len(files))))
                try:
                    result = convert_image(path, settings, output_dir)
                except Exception as error:
                    self.events.put(("failure", (path, str(error))))
                else:
                    self.events.put(("success", result))
                self.events.put(("progress", index))
        except Exception as error:
            self.events.put(("fatal", str(error)))
        finally:
            self.events.put(("done", self.cancel_event.is_set()))

    def _poll_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "discovered":
                    self.progress.configure(maximum=max(1, value))
                elif kind == "start":
                    path, index, total = value
                    self.last_input = path.parent
                    self.status.set(f"圧縮中 {index} / {total}")
                    self.summary.set(path.name)
                elif kind == "success":
                    self._add_success(value)
                elif kind == "failure":
                    self._add_failure(*value)
                elif kind == "fatal":
                    self._add_failure(Path("フォルダーの読み込み"), value)
                elif kind == "progress":
                    self.progress.configure(value=value)
                elif kind == "done":
                    self._finish(value)
                    if self.closing:
                        self._destroy()
                        return
        except queue.Empty:
            pass
        self._poll_id = self.root.after(80, self._poll_events)

    def _add_success(self, result: CompressionResult):
        self.results.append(result)
        self.last_output = result.output_path.parent
        percent = result.savings_percent
        label = f"{percent:.0f}%削減" if percent >= 0 else f"{-percent:.0f}%増加"
        item = self.history.insert("", "end", values=(result.input_path.name, label))
        self.history_details[item] = (
            f"入力: {result.input_path}\n\n出力: {result.output_path}\n\n"
            f"{readable_size(result.input_bytes)} → {readable_size(result.output_bytes)}\n"
            f"画像サイズ: {result.width} × {result.height} px\n{label}\n"
            f"エンコード: {result.backend}"
        )
        self.history.see(item)
        self.history_button.configure(state="normal")
        self.open_button.configure(state="normal")

    def _add_failure(self, path: Path, error: str):
        self.failures.append((path, error))
        item = self.history.insert("", "end", values=(path.name, "失敗"))
        self.history_details[item] = f"{path}\n\n{error}"
        self.history_button.configure(state="normal")

    def _finish(self, cancelled: bool):
        self.busy = False
        successes = len(self.results)
        failures = len(self.failures)
        if not successes and not failures:
            self.status.set("中止しました" if cancelled else "対応画像がありません")
            self.summary.set("AVIF / HEIF / HEIC / JPEG / PNG / WebP / BMP / TIFF / GIF（静止画）")
        else:
            status = f"{successes}枚完了"
            if failures:
                status += f" / {failures}枚失敗"
            if cancelled:
                status += "（中止）"
            self.status.set(status)
            total_input = sum(r.input_bytes for r in self.results)
            total_output = sum(r.output_bytes for r in self.results)
            if total_input:
                percent = (1 - total_output / total_input) * 100
                label = f"{percent:.1f}%削減" if percent >= 0 else f"{-percent:.1f}%増加"
                self.summary.set(f"{readable_size(total_input)} → {readable_size(total_output)}（{label}）")
                if not failures and not cancelled:
                    self.status.set(f"{successes}枚完了（{label}）")
            else:
                self.summary.set("「結果を見る」からエラーを確認できます")
            if failures and not self.history_visible:
                self.toggle_history()
        self.drop_title.configure(text="ここに画像をドロップ" if self.dnd_available else "画像を選んで圧縮")
        self._refresh_controls()

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.status.set("処理後に中止します…")
            self.cancel_button.configure(state="disabled")

    def toggle_history(self):
        if self.history_visible:
            self._hide_history()
        else:
            self.history_visible = True
            self.history_window.deiconify()
            dark_native_caption(self.history_window)
            self.history_window.lift()
            self.history_window.focus_set()

    def _hide_history(self):
        self.history_visible = False
        self.history_window.withdraw()

    def show_result_details(self, _event=None):
        selected = self.history.selection()
        if selected:
            messagebox.showinfo("圧縮結果", self.history_details.get(selected[0], ""), parent=self.root)

    def open_folder(self):
        if self.last_output is None:
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(self.last_output))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.last_output)])
            else:
                subprocess.Popen(["xdg-open", str(self.last_output)])
        except OSError as error:
            messagebox.showerror("保存先を開く", str(error), parent=self.root)

    def _load_settings(self):
        try:
            data = json.loads(settings_path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            self.window_position.load(data.get("window_position"))
            output = data.get("format")
            if output in self.formats:
                self.output_format.set(FORMAT_LABELS[output])
            quality = data.get("quality", 65)
            if isinstance(quality, int) and 1 <= quality <= 100:
                self.quality.set(quality)
                self.quality_text.set(str(quality))
                self.preset.set(next((key for key, value in PRESETS.items() if value == quality), "カスタム"))
            if data.get("speed") in SPEEDS:
                self.speed.set(data["speed"])
            percent = data.get("scale_percent", 100)
            if type(percent) is not int or not 1 <= percent <= 100:
                percent = 100
            self.max_size.set("100%（変更なし）" if percent == 100 else f"{percent}%")
            self.keep_metadata.set(data.get("metadata") is True)
            # Older settings lacked a preservation mode and could have saved
            # quality 65 or a resize. Migrate those installs to safe defaults.
            self.preserve_image.set(data.get("preserve_image") is not False)
            self.overwrite_original.set(data.get("overwrite_original") is True)
            if data.get("folder_mode") in ("same", "custom"):
                self.folder_mode.set(data["folder_mode"])
            if isinstance(data.get("folder"), str) and data["folder"]:
                self.folder.set(data["folder"])
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save_settings(self):
        try:
            percent = self._selected_scale_percent()
        except ValueError:
            percent = 100
        data = {
            "format": self._selected_format(), "quality": round(self.quality.get()),
            "speed": self.speed.get(), "scale_percent": percent,
            "metadata": self.keep_metadata.get(), "folder_mode": self.folder_mode.get(),
            "preserve_image": self.preserve_image.get(),
            "overwrite_original": self.overwrite_original.get(),
            "folder": self.folder.get(),
            "window_position": self.window_position.to_settings(),
        }
        try:
            path = settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass

    def close(self):
        self._save_settings()
        if self.busy:
            self.closing = True
            self.cancel()
            self.status.set("保存後に終了します…")
        else:
            self._destroy()

    def _destroy(self):
        self._save_settings()
        self.root.after_cancel(self._poll_id)
        self.root.destroy()


def create_root() -> tuple[tk.Tk, bool]:
    # This Tk9/X11 combination can abort in the native DnD extension, before a
    # Tcl exception can be raised. File selection still works with plain Tk.
    if sys.platform.startswith("linux") and tk.TkVersion >= 9:
        root, dnd_available = tk.Tk(), False
    else:
        try:
            from tkinterdnd2 import TkinterDnD
            root, dnd_available = TkinterDnD.Tk(), True
        except (ImportError, RuntimeError, tk.TclError):
            if tk._default_root is not None:
                tk._default_root.destroy()
            root, dnd_available = tk.Tk(), False
    root.withdraw()
    return root, dnd_available


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root, dnd_available = create_root()
    try:
        CompressorApp(root, dnd_available)
    except Exception as error:
        messagebox.showerror("起動エラー", str(error), parent=root)
        root.destroy()
        return
    root.mainloop()
