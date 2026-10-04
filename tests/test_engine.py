"""Regression tests covering real codecs and preservation of user files."""

from concurrent.futures import ThreadPoolExecutor
import base64
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PIL import ExifTags, Image, ImageCms
from PIL.PngImagePlugin import PngInfo

from compressor import (
    CompressionResult,
    CompressionSettings,
    available_formats,
    collect_images,
    convert_image,
)


class StartupTests(unittest.TestCase):
    def isolated_check(self, script: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_importing_window_helpers_does_not_load_image_libraries(self) -> None:
        self.isolated_check(
            "import sys; import compressor.window_state; "
            "assert 'compressor.engine' not in sys.modules; "
            "assert 'PIL.Image' not in sys.modules; "
            "from compressor import CompressionSettings; "
            "from compressor.engine import CompressionSettings as EngineSettings; "
            "assert CompressionSettings is EngineSettings; "
            "assert 'pillow_heif' not in sys.modules; "
            "assert 'compressor.hardware' not in sys.modules; "
            "assert 'PIL.ImageCms' not in sys.modules"
        )

    def test_format_probe_loads_only_required_plugins_and_registers_heif(self) -> None:
        self.isolated_check(
            "import sys; from compressor import available_formats; "
            "from PIL import Image; formats = available_formats(); "
            "assert 'PNG' in formats; "
            "assert 'PIL.BlpImagePlugin' not in sys.modules; "
            "assert 'PIL.PdfImagePlugin' not in sys.modules; "
            "assert 'compressor.hardware' not in sys.modules; "
            "assert 'PIL.ImageCms' not in sys.modules; "
            "assert 'HEIF' not in formats or ('HEIF' in Image.SAVE and 'HEIF' in Image.OPEN)"
        )

    def test_direct_heif_import_still_registers_its_opener(self) -> None:
        self.isolated_check(
            "from compressor.heif import register_heif_support; "
            "from PIL import Image; "
            "assert not register_heif_support() or 'HEIF' in Image.OPEN"
        )

    def test_decode_only_and_broken_encoders_are_excluded(self) -> None:
        formats = available_formats()
        real_save = Image.Image.save

        def reject_webp(image, stream, format=None, **options):
            if format == "WEBP":
                raise OSError("decode-only encoder")
            return real_save(image, stream, format=format, **options)

        try:
            available_formats.cache_clear()
            with patch.object(Image.Image, "save", reject_webp):
                self.assertEqual(available_formats(), tuple(item for item in formats if item != "WEBP"))
            available_formats.cache_clear()
            # An imported codec may support decoding but expose no save handler.
            with patch.dict(Image.SAVE, clear=False), patch.object(Image, "init", side_effect=AssertionError("full plugin scan")):
                Image.SAVE.pop("WEBP", None)
                self.assertEqual(available_formats(), tuple(item for item in formats if item != "WEBP"))
        finally:
            available_formats.cache_clear()
            available_formats()


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        # These tests define CPU pixel/color guarantees independently of the
        # host GPU. Hardware routing and validation have their own test suite.
        cpu_only = patch("compressor.engine.try_hardware_encode", return_value=None)
        cpu_only.start()
        self.addCleanup(cpu_only.stop)

    def source_image(
        self, name: str = "source.png", size: tuple[int, int] = (80, 40), mode: str = "RGB"
    ) -> Path:
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new(mode, size, "red") as image:
            image.save(path)
        return path

    @unittest.skipUnless("AVIF" in available_formats(), "AVIF encoder is not installed")
    def test_avif_roundtrip_preserves_transparency_and_can_be_read_as_input(self) -> None:
        source = self.directory / "transparent.png"
        with Image.new("RGBA", (48, 32), (200, 40, 20, 0)) as image:
            image.paste((200, 40, 20, 255), (24, 0, 48, 32))
            image.save(source)
        result = convert_image(source, CompressionSettings())
        self.assertEqual(result.output_path.suffix, ".avif")
        self.assertGreater(result.output_bytes, 0)
        self.assertEqual(result.output_bytes, result.output_path.stat().st_size)
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.format, "AVIF")
            self.assertEqual(encoded.size, (48, 32))
            self.assertEqual(encoded.mode, "RGBA")
            self.assertEqual(encoded.getpixel((4, 4))[3], 0)
            self.assertEqual(encoded.getpixel((40, 4))[3], 255)
        restored = convert_image(result.output_path, CompressionSettings(output_format="PNG"))
        with Image.open(restored.output_path) as encoded:
            self.assertEqual(encoded.size, (48, 32))
            self.assertEqual(encoded.getpixel((4, 4))[3], 0)

    def test_resize_scales_both_dimensions_by_percentage(self) -> None:
        source = self.source_image(size=(200, 100))
        small = convert_image(source, CompressionSettings(output_format="PNG", scale_percent=32, preserve_image=False), self.directory / "small")
        large = convert_image(source, CompressionSettings(output_format="PNG", preserve_image=False), self.directory / "large")
        self.assertEqual((small.width, small.height), (64, 32))
        self.assertEqual((large.width, large.height), (200, 100))
        with Image.open(small.output_path) as encoded:
            self.assertEqual(encoded.size, (64, 32))

    def test_percentage_resize_rounds_half_up_and_keeps_a_minimum_pixel(self) -> None:
        for dimensions, percent, expected in (
            ((5, 3), 50, (3, 2)), ((3, 5), 50, (2, 3)),
            ((150, 250), 1, (2, 3)), ((149, 249), 1, (1, 2)),
            ((1, 100), 1, (1, 1)), ((100, 1), 1, (1, 1)),
        ):
            with self.subTest(dimensions=dimensions, percent=percent):
                source = self.source_image(size=dimensions)
                result = convert_image(source, CompressionSettings(
                    output_format="PNG", scale_percent=percent, preserve_image=False,
                ), self.directory / "output")
                self.assertEqual((result.width, result.height), expected)
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.size, expected)

    def test_percentage_resize_uses_lanczos_and_keeps_alpha_and_color_profile(self) -> None:
        source = self.directory / "alpha-color.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        randomizer = random.Random(209)
        pixels = bytes(randomizer.randrange(256) for _ in range(19 * 13 * 4))
        with Image.frombytes("RGBA", (19, 13), pixels) as image:
            image.save(source, icc_profile=profile)
            with image.resize((10, 7), Image.Resampling.LANCZOS) as resized:
                expected = resized.tobytes()
        original_file = source.read_bytes()
        result = convert_image(source, CompressionSettings(
            output_format="PNG", scale_percent=50, preserve_image=False,
        ), self.directory / "output")
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.size, (10, 7))
            self.assertEqual(encoded.mode, "RGBA")
            self.assertEqual(encoded.tobytes(), expected)
            self.assertEqual(encoded.info["icc_profile"], profile)
        self.assertEqual(source.read_bytes(), original_file)

    def test_jpeg_composites_palette_transparency_on_white(self) -> None:
        source = self.directory / "palette.png"
        with Image.new("P", (64, 32), 0) as image:
            image.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
            image.paste(1, (32, 0, 64, 32))
            image.save(source, transparency=0)
        result = convert_image(source, CompressionSettings(output_format="JPEG", quality=95, preserve_image=False))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.mode, "RGB")
            self.assertTrue(all(component >= 250 for component in encoded.getpixel((8, 16))))
            self.assertGreater(encoded.getpixel((56, 16))[0], 240)
            self.assertLess(encoded.getpixel((56, 16))[1], 10)

    @unittest.skipUnless("WEBP" in available_formats(), "WebP encoder is not installed")
    def test_webp_and_png_keep_alpha(self) -> None:
        source = self.directory / "alpha.png"
        with Image.new("RGBA", (24, 16), (40, 100, 200, 73)) as image:
            image.save(source)
        for output_format in ("WEBP", "PNG"):
            with self.subTest(output_format=output_format):
                result = convert_image(source, CompressionSettings(output_format=output_format), self.directory / "output")
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.getpixel((10, 10))[3], 73)

    def test_recompression_overwrites_existing_output_with_unsuffixed_name(self) -> None:
        source = self.source_image("photo.png")
        original = source.read_bytes()
        output_dir = self.directory / "output"
        output_dir.mkdir()
        destination = output_dir / "photo.png"
        destination.write_bytes(b"older output")
        first = convert_image(source, CompressionSettings(output_format="PNG"), output_dir)
        second = convert_image(source, CompressionSettings(output_format="PNG"), output_dir)
        self.assertEqual(first.output_path, destination)
        self.assertEqual(second.output_path, destination)
        with Image.open(destination) as encoded:
            self.assertEqual(encoded.size, (80, 40))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(output_dir.iterdir()), [destination])

    def test_parallel_conversions_atomically_publish_to_the_same_output(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        settings = CompressionSettings(output_format="PNG")
        output_dir = self.directory / "output"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: convert_image(source, settings, output_dir), range(8)))
        self.assertEqual({result.output_path for result in results}, {output_dir / "source.png"})
        for result in results:
            with Image.open(result.output_path) as encoded:
                encoded.load()
                self.assertEqual(encoded.size, (80, 40))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(output_dir.iterdir()), [output_dir / "source.png"])

    def test_same_format_overwrites_source_only_after_valid_encoding(self) -> None:
        source = self.source_image(size=(40, 20))
        original_bytes = source.read_bytes()
        with Image.open(source) as original:
            pixels = original.tobytes()
        result = convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        self.assertEqual(result.output_path, source)
        self.assertEqual(result.input_bytes, len(original_bytes))
        with Image.open(source) as encoded:
            self.assertEqual(encoded.tobytes(), pixels)
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_original_replacement_defaults_off_and_same_path_is_rejected(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        self.assertFalse(CompressionSettings().overwrite_original)
        with self.assertRaisesRegex(ValueError, "元ファイルを上書き.*別の保存先"):
            convert_image(source, CompressionSettings(output_format="PNG"))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_original_replacement_in_different_format_uses_the_source_folder(self) -> None:
        source = self.source_image(size=(80, 40))
        ignored_folder = self.directory / "selected output"
        original_size = source.stat().st_size
        result = convert_image(source, CompressionSettings(
            output_format="JPEG", preserve_image=False, scale_percent=50,
            overwrite_original=True,
        ), ignored_folder)
        self.assertEqual(result.output_path, self.directory / "source.jpg")
        self.assertEqual(result.input_bytes, original_size)
        self.assertFalse(source.exists())
        self.assertFalse(ignored_folder.exists())
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.format, "JPEG")
            self.assertEqual(encoded.size, (40, 20))
        self.assertEqual(list(self.directory.iterdir()), [result.output_path])

    def test_original_replacement_removes_source_after_overwriting_a_previous_output(self) -> None:
        source = self.source_image()
        destination = self.directory / "source.webp"
        destination.write_bytes(b"previous output")
        result = convert_image(source, CompressionSettings(
            output_format="WEBP", overwrite_original=True,
        ))
        self.assertEqual(result.output_path, destination)
        self.assertFalse(source.exists())
        with Image.open(destination) as encoded:
            self.assertEqual(encoded.size, (80, 40))
        self.assertEqual(list(self.directory.iterdir()), [destination])

    def test_failed_source_removal_rolls_back_existing_output_or_removes_new_output(self) -> None:
        real_unlink = Path.unlink
        for existing_output in (False, True):
            with self.subTest(existing_output=existing_output):
                source = self.source_image()
                original = source.read_bytes()
                destination = self.directory / "source.webp"
                previous = b"previous output must survive"
                if existing_output:
                    destination.write_bytes(previous)

                def reject_source(path, *args, **kwargs):
                    if path == source:
                        raise PermissionError("source is locked")
                    return real_unlink(path, *args, **kwargs)

                with patch.object(Path, "unlink", reject_source):
                    with self.assertRaisesRegex(ValueError, "保存できません.*source is locked"):
                        convert_image(source, CompressionSettings(
                            output_format="WEBP", overwrite_original=True,
                        ))
                self.assertEqual(source.read_bytes(), original)
                if existing_output:
                    self.assertEqual(destination.read_bytes(), previous)
                else:
                    self.assertFalse(destination.exists())
                self.assertEqual(set(self.directory.iterdir()), {source, destination} if existing_output else {source})

    def test_original_replacement_failures_before_source_removal_leave_both_files_unchanged(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        destination = self.directory / "source.webp"
        previous = b"previous output must survive"
        destination.write_bytes(previous)
        for target, error in (
            ("compressor.engine._verify_preserved_image", ValueError("validation failed")),
            ("compressor.engine.shutil.copy2", OSError("backup failed")),
            ("compressor.engine.os.replace", OSError("publication failed")),
        ):
            with self.subTest(target=target):
                with patch(target, side_effect=error), self.assertRaises(ValueError):
                    convert_image(source, CompressionSettings(
                        output_format="WEBP", overwrite_original=True,
                    ))
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(destination.read_bytes(), previous)
                self.assertEqual(set(self.directory.iterdir()), {source, destination})

    def test_original_replacement_rejects_invalid_lossy_cpu_outputs(self) -> None:
        source = self.source_image("original.bmp")
        real_save = Image.Image.save
        for output_format, extension in (("JPEG", ".jpg"), ("WEBP", ".webp"), ("PNG", ".png")):
            destination = self.directory / (source.stem + extension)
            destination.write_bytes(b"previous output must survive")
            existing_files = {path: path.read_bytes() for path in self.directory.iterdir()}
            for fault in ("undecodable", "wrong size", "wrong format"):
                with self.subTest(output_format=output_format, fault=fault):
                    def invalid_save(image, path, *args, **kwargs):
                        if fault == "undecodable":
                            Path(path).write_bytes(b"truncated image")
                        else:
                            size = (1, 1) if fault == "wrong size" else image.size
                            with Image.new("RGB", size, "red") as altered:
                                real_save(altered, path, format="BMP" if fault == "wrong format" else output_format)

                    with patch.object(Image.Image, "save", invalid_save), self.assertRaises(ValueError):
                        convert_image(source, CompressionSettings(
                            output_format=output_format, preserve_image=False, overwrite_original=True,
                        ))
                    self.assertEqual({path: path.read_bytes() for path in self.directory.iterdir()}, existing_files)

    def test_failed_source_removal_and_failed_rollback_keep_the_original_and_recovery_copy(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        destination = self.directory / "source.webp"
        previous = b"previous output must remain recoverable"
        destination.write_bytes(previous)
        real_unlink, real_replace = Path.unlink, os.replace
        replacements = []

        def reject_source(path, *args, **kwargs):
            if path == source:
                raise PermissionError("source is locked")
            return real_unlink(path, *args, **kwargs)

        def reject_rollback(first, second):
            replacements.append(first)
            if len(replacements) == 2:
                raise PermissionError("rollback is locked")
            return real_replace(first, second)

        with patch.object(Path, "unlink", reject_source), patch("compressor.engine.os.replace", reject_rollback):
            with self.assertRaisesRegex(ValueError, "元画像は残っています.*", msg="the recovery path must be reported") as failure:
                convert_image(source, CompressionSettings(output_format="WEBP", overwrite_original=True))
        backups = list(self.directory.glob(".image-compressor-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), previous)
        self.assertIn(str(backups[0]), str(failure.exception))
        self.assertEqual(source.read_bytes(), original)
        with Image.open(destination) as encoded:
            self.assertEqual(encoded.size, (80, 40))
        self.assertFalse(list(self.directory.glob(".image-compressor-*.tmp")))

    def test_failed_encoding_preserves_existing_destination(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        destination = self.directory / "source.jpg"
        previous = b"previous output must survive"
        destination.write_bytes(previous)
        available_formats()
        with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "保存できません"):
                convert_image(source, CompressionSettings(output_format="JPEG", preserve_image=False))
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(set(self.directory.iterdir()), {source, destination})

    def test_failed_validation_preserves_existing_destination(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        destination = self.directory / "source.webp"
        previous = b"previous output must survive"
        destination.write_bytes(previous)
        with patch("compressor.engine._verify_preserved_image", side_effect=ValueError("validation failed")):
            with self.assertRaisesRegex(ValueError, "validation failed"):
                convert_image(source, CompressionSettings(output_format="WEBP"))
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(set(self.directory.iterdir()), {source, destination})

    def test_failed_replace_preserves_existing_destination(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        destination = self.directory / "source.webp"
        previous = b"previous output must survive"
        destination.write_bytes(previous)
        with patch("compressor.engine.os.replace", side_effect=OSError("permission denied")):
            with self.assertRaisesRegex(ValueError, "保存できません"):
                convert_image(source, CompressionSettings(output_format="WEBP"))
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(set(self.directory.iterdir()), {source, destination})

    def test_failed_encode_removes_reserved_and_temporary_files(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        available_formats()
        with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "保存できません"):
                convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        self.assertEqual(list(self.directory.iterdir()), [source])
        self.assertEqual(source.read_bytes(), original)

    def test_failed_atomic_replace_cleans_up_without_touching_source(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        with patch("compressor.engine.os.replace", side_effect=OSError("permission denied")):
            with self.assertRaisesRegex(ValueError, "保存できません"):
                convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        self.assertEqual(list(self.directory.iterdir()), [source])
        self.assertEqual(source.read_bytes(), original)

    def test_windows_publication_retries_only_bounded_sharing_failures(self) -> None:
        source = self.source_image()
        real_replace = os.replace
        transient = PermissionError("temporary scanner lock")
        transient.winerror = 32
        attempts = []

        def briefly_locked(first, second):
            attempts.append(first)
            if len(attempts) == 1:
                raise transient
            return real_replace(first, second)

        with patch("compressor.engine.sys.platform", "win32"), \
                patch("compressor.engine.os.replace", briefly_locked), \
                patch("compressor.engine.time.sleep") as slept:
            result = convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        self.assertEqual(result.output_path, source)
        self.assertEqual(len(attempts), 2)
        slept.assert_called_once_with(0.025)
        original = source.read_bytes()
        permanent = PermissionError("permanent access denied")
        permanent.winerror = 5
        with patch("compressor.engine.sys.platform", "win32"), \
                patch("compressor.engine.os.replace", side_effect=permanent) as replace, \
                patch("compressor.engine.time.sleep") as slept:
            with self.assertRaisesRegex(ValueError, "保存できません"):
                convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(slept.call_count, 4)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_output_directory_is_created(self) -> None:
        source = self.source_image()
        output_dir = self.directory / "nested" / "output"
        result = convert_image(source, CompressionSettings(output_format="PNG"), output_dir)
        self.assertEqual(result.output_path.parent, output_dir)
        self.assertTrue(result.output_path.is_file())

    def test_metadata_is_removed_by_default_but_color_profile_is_kept(self) -> None:
        source = self.directory / "metadata.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        exif = Image.Exif()
        exif[ExifTags.Base.Make] = "Private Camera"
        exif[ExifTags.IFD.GPSInfo] = {1: "N", 2: (35.0, 0.0, 0.0)}
        png_info = PngInfo()
        png_info.add_text("Author", "Private Name")
        png_info.add_itxt("XML:com.adobe.xmp", "private location")
        with Image.new("RGB", (20, 30), "red") as image:
            image.save(source, exif=exif, icc_profile=profile, pnginfo=png_info)
        for output_format in available_formats():
            with self.subTest(output_format=output_format):
                result = convert_image(source, CompressionSettings(output_format=output_format, preserve_image=output_format != "JPEG"), self.directory / "output")
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(len(encoded.getexif()), 0)
                    self.assertNotIn("xmp", encoded.info)
                    self.assertNotIn("XML:com.adobe.xmp", encoded.info)
                    self.assertNotIn("Author", encoded.info)
                    self.assertEqual(encoded.info.get("icc_profile"), profile)

    def test_preserved_exif_has_correct_orientation_and_resized_dimensions(self) -> None:
        source = self.directory / "rotated.png"
        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = 6
        exif[ExifTags.Base.Make] = "Test Camera"
        exif[ExifTags.Base.ImageWidth] = 30
        exif[ExifTags.Base.ImageLength] = 20
        exif[ExifTags.IFD.Exif] = {
            ExifTags.Base.ExifImageWidth: 30,
            ExifTags.Base.ExifImageHeight: 20,
        }
        with Image.new("RGB", (30, 20), "red") as image:
            image.save(source, exif=exif)
        result = convert_image(
            source, CompressionSettings(output_format="PNG", scale_percent=33, preserve_metadata=True, preserve_image=False), self.directory / "output"
        )
        self.assertEqual((result.width, result.height), (7, 10))
        with Image.open(result.output_path) as encoded:
            encoded_exif = encoded.getexif()
            self.assertNotIn(ExifTags.Base.Orientation, encoded_exif)
            self.assertEqual(encoded_exif[ExifTags.Base.Make], "Test Camera")
            self.assertEqual(encoded_exif[ExifTags.Base.ImageWidth], 7)
            self.assertEqual(encoded_exif[ExifTags.Base.ImageLength], 10)
            dimensions = encoded_exif.get_ifd(ExifTags.IFD.Exif)
            self.assertEqual(dimensions[ExifTags.Base.ExifImageWidth], 7)
            self.assertEqual(dimensions[ExifTags.Base.ExifImageHeight], 10)

    def test_tiff_original_replacement_keeps_photo_exif_color_and_resized_alpha(self) -> None:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for nested in (False, True):
            with self.subTest(nested=nested):
                source = self.directory / ("nested.tiff" if nested else "camera.tiff")
                exif = Image.Exif()
                exif[ExifTags.Base.Make] = "Independent transaction review"
                if nested:
                    exif[ExifTags.Base.Orientation] = 6
                    exif[ExifTags.IFD.Exif] = {
                        ExifTags.Base.DateTimeOriginal: "2026:10:04 12:00:00",
                        ExifTags.Base.ExifImageWidth: 64,
                        ExifTags.Base.ExifImageHeight: 32,
                    }
                    exif[ExifTags.IFD.GPSInfo] = {1: "N", 2: (35.0, 0.0, 0.0)}
                with Image.new("RGBA", (64, 32), (40, 110, 180, 128)) as image:
                    image.putpixel((0, 0), (220, 30, 60, 200))
                    image.putpixel((63, 31), (20, 210, 90, 64))
                    image.save(source, icc_profile=profile, exif=exif.tobytes())
                    with image.transpose(Image.Transpose.ROTATE_270) if nested else image.copy() as oriented:
                        dimensions = (16, 32) if nested else (32, 16)
                        with oriented.resize(dimensions, Image.Resampling.LANCZOS) as resized:
                            expected_pixels = resized.tobytes()
                result = convert_image(source, CompressionSettings(
                    output_format="PNG", preserve_image=False, scale_percent=50,
                    preserve_metadata=True, overwrite_original=True,
                ))
                self.assertFalse(source.exists())
                self.assertEqual(result.output_path, source.with_suffix(".png"))
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.size, dimensions)
                    self.assertEqual(encoded.tobytes(), expected_pixels)
                    self.assertEqual(encoded.info["icc_profile"], profile)
                    saved_exif = encoded.getexif()
                    self.assertEqual(saved_exif[ExifTags.Base.Make], "Independent transaction review")
                    self.assertNotIn(ExifTags.Base.Orientation, saved_exif)
                    self.assertEqual(saved_exif[ExifTags.Base.ImageWidth], dimensions[0])
                    self.assertEqual(saved_exif[ExifTags.Base.ImageLength], dimensions[1])
                    for structural_tag in (258, 259, 262, 273, 277, 278, 279, 284, 322, 323, 324, 325, 338):
                        self.assertNotIn(structural_tag, saved_exif)
                    if nested:
                        photo = saved_exif.get_ifd(ExifTags.IFD.Exif)
                        self.assertEqual(photo[ExifTags.Base.DateTimeOriginal], "2026:10:04 12:00:00")
                        self.assertEqual(photo[ExifTags.Base.ExifImageWidth], dimensions[0])
                        self.assertEqual(photo[ExifTags.Base.ExifImageHeight], dimensions[1])
                        self.assertEqual(saved_exif.get_ifd(ExifTags.IFD.GPSInfo)[1], "N")

    def test_tiff_photo_exif_can_still_be_removed(self) -> None:
        source = self.directory / "private.tiff"
        exif = Image.Exif()
        exif[ExifTags.Base.Make] = "Private Camera"
        exif[ExifTags.IFD.GPSInfo] = {1: "N", 2: (35.0, 0.0, 0.0)}
        with Image.new("RGB", (16, 8), "red") as image:
            image.save(source, exif=exif.tobytes())
        result = convert_image(source, CompressionSettings(output_format="PNG"))
        with Image.open(result.output_path) as encoded:
            self.assertFalse(encoded.getexif())

    def test_animation_is_rejected_without_silently_saving_first_frame(self) -> None:
        source = self.directory / "animated.gif"
        with Image.new("RGB", (20, 20), "red") as first, Image.new("RGB", (20, 20), "blue") as second:
            first.save(source, save_all=True, append_images=[second], duration=100, loop=0)
        with self.assertRaisesRegex(ValueError, "アニメーション"):
            convert_image(source, CompressionSettings(output_format="PNG"))
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_collect_images_recurses_and_deduplicates_in_deterministic_order(self) -> None:
        second = self.source_image("b.PNG")
        first = self.source_image("a.png")
        nested = self.source_image("nested/c.jpg")
        unsupported = self.directory / "notes.txt"
        unsupported.write_text("not an image", encoding="utf-8")
        self.assertEqual(
            collect_images([self.directory, second, self.directory / "missing.png", unsupported]),
            [first, second, nested],
        )

    def test_invalid_settings_and_input_errors_are_clear(self) -> None:
        for values in (
            {"output_format": "GIF"}, {"quality": -1}, {"quality": 101}, {"quality": True},
            {"speed": 11}, {"speed": 1.5},
            {"scale_percent": 0}, {"scale_percent": 101}, {"scale_percent": True},
            {"scale_percent": 50.0}, {"scale_percent": "50"}, {"scale_percent": None},
            {"preserve_metadata": "yes"},
            {"preserve_image": "yes"},
            {"overwrite_original": "yes"}, {"overwrite_original": 1},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                CompressionSettings(**values)
        with self.assertRaisesRegex(FileNotFoundError, "見つかりません"):
            convert_image(self.directory / "missing.png", CompressionSettings())
        corrupt = self.directory / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "読み込めません"):
            convert_image(corrupt, CompressionSettings())
        unsupported = self.directory / "source.txt"
        unsupported.write_text("not an image", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "対応していない"):
            convert_image(unsupported, CompressionSettings())

    def test_savings_can_be_negative_for_larger_outputs(self) -> None:
        result = CompressionResult(Path("a"), Path("b"), 100, 150, 10, 10)
        self.assertEqual(result.savings_percent, -50)

    def test_preservation_defaults_keep_every_pixel_and_resolution_in_all_lossless_formats(self) -> None:
        """Noise, single-pixel colors and hidden RGB exercise actual codec losslessness."""
        self.assertTrue(CompressionSettings().preserve_image)
        source = self.directory / "all_channels.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        randomizer = random.Random(942)
        pixels = bytes(randomizer.randrange(256) for _ in range(41 * 29 * 4))
        with Image.frombytes("RGBA", (41, 29), pixels) as image:
            for x in range(41):
                image.putpixel((x, 0), ((x * 73) % 256, (x * 29) % 256, 255 - x, 0))
            image.save(source, icc_profile=profile)
            pixels = image.tobytes()
        original_file = source.read_bytes()
        for output_format in ("AVIF", "HEIF", "WEBP", "PNG"):
            if output_format not in available_formats():
                continue
            with self.subTest(output_format=output_format):
                result = convert_image(source, CompressionSettings(
                    output_format=output_format, quality=0, scale_percent=1, speed=10,
                ), self.directory / "preserved")
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.size, (41, 29))
                    self.assertEqual(encoded.convert("RGBA").tobytes(), pixels)
                    self.assertEqual(encoded.info.get("icc_profile"), profile)
        self.assertEqual(source.read_bytes(), original_file)

    def test_jpeg_preservation_is_rejected_without_creating_output(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        with self.assertRaisesRegex(ValueError, "JPEG.*完全に保持できません"):
            convert_image(source, CompressionSettings(output_format="JPEG"))
        self.assertEqual(list(self.directory.iterdir()), [source])
        self.assertEqual(source.read_bytes(), original)

    def test_png_keeps_palette_and_partial_transparency(self) -> None:
        source = self.directory / "indexed.png"
        with Image.new("P", (5, 1)) as image:
            image.putpalette([17, 201, 53, 251, 11, 240, 105, 61, 29] + [0] * 759)
            image.putdata([0, 1, 2, 0, 1])
            image.save(source, transparency=bytes([0, 73, 255]))
        result = convert_image(source, CompressionSettings(output_format="PNG"), self.directory / "output")
        with Image.open(source) as original, Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.mode, "P")
            self.assertEqual(encoded.tobytes(), original.tobytes())
            self.assertEqual(encoded.getpalette(), original.getpalette())
            self.assertEqual(encoded.info["transparency"], original.info["transparency"])
            self.assertEqual(encoded.convert("RGBA").tobytes(), original.convert("RGBA").tobytes())

    def test_png_keeps_16_bit_grayscale_values(self) -> None:
        source = self.directory / "sixteen-bit.png"
        values = (0, 1, 128, 255, 256, 32768, 65534, 65535)
        with Image.frombytes("I;16", (8, 1), struct.pack("<8H", *values)) as image:
            image.save(source)
        result = convert_image(source, CompressionSettings(output_format="PNG"), self.directory / "output")
        with Image.open(result.output_path) as encoded:
            self.assertEqual(tuple(encoded.get_flattened_data()), values)
        for output_format in ("AVIF", "HEIF", "WEBP"):
            with self.subTest(output_format=output_format), self.assertRaisesRegex(ValueError, "16 ビット"):
                convert_image(source, CompressionSettings(output_format=output_format))

    def test_16_bit_color_png_is_rejected_before_decoder_truncation_can_be_published(self) -> None:
        source = self.directory / "sixteen-bit-color.png"

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        source.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 16, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + struct.pack(">3H", 257, 32769, 65534)))
            + chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(ValueError, "16 ビットのカラー画像"):
            convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        with self.assertRaisesRegex(ValueError, "16 ビットのカラー画像"):
            convert_image(source, CompressionSettings(preserve_image=False))
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_png_keeps_color_interpretation_chunks_even_when_private_metadata_is_removed(self) -> None:
        source = self.directory / "gamma.png"
        chunks = {
            b"gAMA": struct.pack(">I", 100000),
            b"cHRM": struct.pack(">8I", 31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000),
            b"cICP": b"\x09\x10\x00\x01",
            b"sBIT": bytes((8, 8, 8)),
        }
        png_info = PngInfo()
        for kind, data in chunks.items():
            png_info.add(kind, data)
        png_info.add_text("Author", "Private Name")
        with Image.new("RGB", (5, 4), (50, 73, 201)) as image:
            image.save(source, pnginfo=png_info)
        result = convert_image(source, CompressionSettings(output_format="PNG"), self.directory / "output")
        with Image.open(source) as original, Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.info["gamma"], original.info["gamma"])
            self.assertEqual(encoded.info["chromaticity"], original.info["chromaticity"])
            self.assertEqual(encoded.tobytes(), original.tobytes())
            self.assertNotIn("Author", encoded.info)
        from compressor.engine import _png_color_chunks
        self.assertEqual(dict(_png_color_chunks(result.output_path)[1]), chunks)
        for output_format in ("AVIF", "WEBP"):
            with self.subTest(output_format=output_format), self.assertRaisesRegex(ValueError, "色情報"):
                convert_image(source, CompressionSettings(output_format=output_format))

    def test_png_gamma_without_icc_is_not_silently_discarded_in_other_formats(self) -> None:
        source = self.directory / "linear.png"
        png_info = PngInfo()
        png_info.add(b"gAMA", struct.pack(">I", 100000))
        with Image.new("RGB", (12, 8), (82, 190, 37)) as image:
            image.save(source, pnginfo=png_info)
        for output_format in ("AVIF", "WEBP"):
            with self.subTest(output_format=output_format), self.assertRaisesRegex(ValueError, "ガンマ"):
                convert_image(source, CompressionSettings(output_format=output_format))
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_png_srgb_declaration_is_carried_as_an_srgb_profile(self) -> None:
        source = self.directory / "declared-srgb.png"
        png_info = PngInfo()
        png_info.add(b"sRGB", b"\0")
        png_info.add(b"gAMA", struct.pack(">I", 45455))
        with Image.new("RGB", (12, 8), (82, 190, 37)) as image:
            image.save(source, pnginfo=png_info)
        for output_format in ("AVIF", "WEBP"):
            if output_format not in available_formats():
                continue
            with self.subTest(output_format=output_format):
                result = convert_image(source, CompressionSettings(output_format=output_format), self.directory / "output")
                with Image.open(source) as original, Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.convert("RGB").tobytes(), original.tobytes())
                    self.assertTrue(encoded.info.get("icc_profile"))

    def test_preservation_applies_exif_orientation_without_resizing(self) -> None:
        source = self.directory / "oriented.png"
        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = 6
        with Image.new("RGB", (3, 2)) as image:
            image.putdata([(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120), (130, 140, 150), (160, 170, 180)])
            image.save(source, exif=exif)
            expected = image.transpose(Image.Transpose.ROTATE_270).tobytes()
        result = convert_image(source, CompressionSettings(output_format="PNG", scale_percent=1, preserve_metadata=True), self.directory / "output")
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.size, (2, 3))
            self.assertEqual(encoded.tobytes(), expected)
            self.assertNotIn(ExifTags.Base.Orientation, encoded.getexif())

    def test_output_pixel_validation_rejects_modified_encoding_and_cleans_up(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        real_save = Image.Image.save

        available_formats()
        for fault in ("modified pixels", "wrong format"):
            with self.subTest(fault=fault):
                def altered_save(image: Image.Image, destination: Path, *args: object, **kwargs: object) -> None:
                    with image.copy() as altered:
                        if fault == "modified pixels":
                            altered.putpixel((0, 0), (1, 2, 3))
                            real_save(altered, destination, *args, **kwargs)
                        else:
                            real_save(altered, destination, format="BMP")

                with patch.object(Image.Image, "save", altered_save), self.assertRaisesRegex(ValueError, "一致しません"):
                    convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
                self.assertEqual(list(self.directory.iterdir()), [source])
                self.assertEqual(source.read_bytes(), original)

    @unittest.skipUnless("AVIF" in available_formats(), "AVIF decoder is not installed")
    def test_high_depth_avif_is_rejected_even_when_pillow_reports_rgb_8_bit_mode(self) -> None:
        from compressor.avif_lossless import _encoder_path
        png_source = self.source_image(size=(8, 4))
        source = self.directory / "ten-bit.avif"
        subprocess.run([
            str(_encoder_path()), "--lossless", "--depth", "10", "--speed", "10",
            "--jobs", "1", "--", str(png_source), str(source),
        ], capture_output=True, check=True)
        with Image.open(source) as image:
            self.assertEqual(image.mode, "RGB")
        original = source.read_bytes()
        with self.assertRaisesRegex(ValueError, "高ビット深度の AVIF"):
            convert_image(source, CompressionSettings(output_format="PNG"))
        with self.assertRaisesRegex(ValueError, "高ビット深度の AVIF"):
            convert_image(source, CompressionSettings(preserve_image=False, overwrite_original=True))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(self.directory.glob(".image-compressor-*")))

    @unittest.skipUnless("AVIF" in available_formats(), "AVIF decoder is not installed")
    def test_hdr_avif_color_information_is_not_silently_changed_to_srgb(self) -> None:
        from compressor.avif_lossless import _encoder_path
        png_source = self.source_image(size=(8, 4))
        source = self.directory / "hdr.avif"
        subprocess.run([
            str(_encoder_path()), "--lossless", "--cicp", "9/16/0", "--speed", "10",
            "--jobs", "1", "--", str(png_source), str(source),
        ], capture_output=True, check=True)
        original = source.read_bytes()
        with self.assertRaisesRegex(ValueError, "色情報"):
            convert_image(source, CompressionSettings(output_format="AVIF", overwrite_original=True))
        with self.assertRaisesRegex(ValueError, "色情報"):
            convert_image(source, CompressionSettings(preserve_image=False, overwrite_original=True))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(self.directory.glob(".image-compressor-*")))

    def test_grayscale_icc_stays_attached_to_grayscale_pixels(self) -> None:
        # A valid, generated LCMS gray profile with gamma 2.2 and a D50 white point.
        profile = base64.b64decode(
            "AAABXGxjbXMEQAAAbW50ckdSQVlYWVogB+oACgADABYAOAAsYWNzcEFQUEwAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAPbWAAEAAAAA0y1sY21zAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAAAAAAEZGVzYwAAALQAAAA2Y3BydAAAAOwAAABMd3RwdAAAATgA"
            "AAAUa1RSQwAAAUwAAAAQbWx1YwAAAAAAAAABAAAADGVuVVMAAAAaAAAAHABnAHIAYQB5ACAAYgB1"
            "AGkAbAB0AC0AaQBuAABtbHVjAAAAAAAAAAEAAAAMZW5VUwAAADAAAAAcAE4AbwAgAGMAbwBwAHkA"
            "cgBpAGcAaAB0ACwAIAB1AHMAZQAgAGYAcgBlAGUAbAB5WFlaIAAAAAAAAPbcAAEAAAAA0zpwYXJh"
            "AAAAAAAAAAAAAjMz"
        )
        source = self.directory / "gray-profile.png"
        pixels = bytes(range(256))
        with Image.frombytes("L", (32, 8), pixels) as image:
            image.save(source, icc_profile=profile)
        for output_format in ("PNG", "AVIF", "HEIF"):
            if output_format not in available_formats():
                continue
            with self.subTest(output_format=output_format):
                result = convert_image(source, CompressionSettings(output_format=output_format), self.directory / "output")
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.mode, "L")
                    self.assertEqual(encoded.tobytes(), pixels)
                    self.assertEqual(encoded.info.get("icc_profile"), profile)
        with self.assertRaisesRegex(ValueError, "グレースケール.*PNG"):
            convert_image(source, CompressionSettings(output_format="WEBP"))
        result = convert_image(source, CompressionSettings(preserve_image=False, quality=85))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.mode, "L")
            self.assertEqual(encoded.info.get("icc_profile"), profile)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(encoded.tobytes(), pixels)), 8)
        from compressor.engine import _avif_properties
        self.assertIn(b"colrnclx" + struct.pack(">HHHB", 2, 2, 6, 128), _avif_properties(result.output_path))

    def test_invalid_or_incompatible_icc_is_not_attached_to_different_color_values(self) -> None:
        source = self.directory / "incompatible-profile.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        with Image.new("L", (8, 4), 128) as image:
            image.save(source, icc_profile=profile)
        with self.assertRaisesRegex(ValueError, "種類が一致しません"):
            convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        with Image.new("RGB", (8, 4), (100, 20, 70)) as image:
            image.save(source, icc_profile=b"not a color profile")
        with self.assertRaisesRegex(ValueError, "カラープロファイルを読み込めません"):
            convert_image(source, CompressionSettings(output_format="PNG", overwrite_original=True))
        self.assertFalse(list(self.directory.glob(".image-compressor-*")))

    def test_wide_gamut_profile_and_pixels_are_not_replaced_by_srgb(self) -> None:
        # Generated LCMS RGB profile: Adobe RGB primaries, D65 and gamma 2.2.
        profile = base64.b64decode(
            "AAACOGxjbXMEQAAAbW50clJHQiBYWVogB+oACgADABYAOQASYWNzcEFQUEwAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAPbWAAEAAAAA0y1sY21zAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAAAAAALZGVzYwAAAQgAAAA0Y3BydAAAATwAAABMd3RwdAAAAYgA"
            "AAAUY2hhZAAAAZwAAAAsclhZWgAAAcgAAAAUYlhZWgAAAdwAAAAUZ1hZWgAAAfAAAAAUclRSQwAA"
            "AgQAAAAQZ1RSQwAAAgQAAAAQYlRSQwAAAgQAAAAQY2hybQAAAhQAAAAkbWx1YwAAAAAAAAABAAAA"
            "DGVuVVMAAAAYAAAAHABSAEcAQgAgAGIAdQBpAGwAdAAtAGkAbm1sdWMAAAAAAAAAAQAAAAxlblVT"
            "AAAAMAAAABwATgBvACAAYwBvAHAAeQByAGkAZwBoAHQALAAgAHUAcwBlACAAZgByAGUAZQBsAHlY"
            "WVogAAAAAAAA9tYAAQAAAADTLXNmMzIAAAAAAAEMQgAABd7///MlAAAHkwAA/ZD///uh///9ogAAA9wA"
            "AMBuWFlaIAAAAAAAAJwYAABPpQAABPxYWVogAAAAAAAAJjEAABAvAAC+m1hZWiAAAAAAAAA0jQAAoCwA"
            "AA+VcGFyYQAAAAAAAAAAAAIzM2Nocm0AAAAAAAMAAAAAo9cAAFR7AAA1wwAAtcMAACZmAAAPXA=="
        )
        source = self.directory / "wide-gamut.png"
        randomizer = random.Random(39)
        pixels = bytes(randomizer.randrange(256) for _ in range(23 * 11 * 4))
        with Image.frombytes("RGBA", (23, 11), pixels) as image:
            image.save(source, icc_profile=profile)
        for output_format in ("AVIF", "HEIF", "WEBP", "PNG"):
            if output_format not in available_formats():
                continue
            with self.subTest(output_format=output_format):
                result = convert_image(source, CompressionSettings(output_format=output_format), self.directory / "output")
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.convert("RGBA").tobytes(), pixels)
                    self.assertEqual(encoded.info.get("icc_profile"), profile)
        result = convert_image(source, CompressionSettings(preserve_image=False, quality=85))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.info.get("icc_profile"), profile)
            self.assertEqual(encoded.convert("RGBA").getchannel("A").tobytes(), pixels[3::4])
        from compressor.engine import _avif_properties
        self.assertIn(b"colrnclx" + struct.pack(">HHHB", 2, 2, 0, 128), _avif_properties(result.output_path))

    def test_adobe_rgb_exif_without_icc_is_not_silently_discarded(self) -> None:
        source = self.directory / "exif-adobe-rgb.jpg"
        exif = Image.Exif()
        exif[ExifTags.IFD.Exif] = {ExifTags.Base.ColorSpace: 2}
        with Image.new("RGB", (8, 4), (47, 190, 71)) as image:
            image.save(source, exif=exif)
        with self.assertRaisesRegex(ValueError, "Adobe RGB"):
            convert_image(source, CompressionSettings(output_format="AVIF"))
        with self.assertRaisesRegex(ValueError, "Adobe RGB"):
            convert_image(source, CompressionSettings(preserve_image=False))
        self.assertFalse(list(self.directory.glob(".image-compressor-*")))

    @unittest.skipUnless("AVIF" in available_formats(), "AVIF decoder is not installed")
    def test_srgb_grayscale_declaration_uses_matching_rgb_pixels_and_profile_in_avif(self) -> None:
        source = self.directory / "gray-declared-srgb.png"
        png_info = PngInfo()
        png_info.add(b"sRGB", b"\0")
        pixels = bytes(range(256))
        with Image.frombytes("L", (32, 8), pixels) as image:
            image.save(source, pnginfo=png_info)
            expected_rgb = image.convert("RGB").tobytes()
        result = convert_image(source, CompressionSettings(output_format="AVIF"))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.mode, "RGB")
            self.assertEqual(encoded.tobytes(), expected_rgb)
            import io
            profile = ImageCms.ImageCmsProfile(io.BytesIO(encoded.info["icc_profile"]))
            self.assertEqual(profile.profile.xcolor_space.strip(), "RGB")

    def test_32_bit_tiff_is_not_reinterpreted_as_16_bit_png_even_when_values_fit(self) -> None:
        source = self.directory / "thirty-two-bit.tiff"
        with Image.new("I", (4, 2), 32768) as image:
            image.save(source)
        with self.assertRaisesRegex(ValueError, "32 ビットの TIFF"):
            convert_image(source, CompressionSettings(output_format="PNG"))
        self.assertEqual(list(self.directory.iterdir()), [source])

    @unittest.skipUnless("AVIF" in available_formats(), "AVIF decoder is not installed")
    def test_lossy_avif_retains_single_pixel_saturated_colors_much_better_than_old_420(self) -> None:
        source = self.directory / "saturated-stripes.png"
        old_output = self.directory / "old-420.avif"
        colors = ((255, 0, 0), (0, 255, 255))
        with Image.new("RGB", (64, 32)) as image:
            image.putdata([colors[x % 2] for _y in range(32) for x in range(64)])
            image.save(source)
            image.save(old_output, format="AVIF", quality=85, speed=10, max_threads=1)
            pixels = image.tobytes()
        original_file = source.read_bytes()
        result = convert_image(source, CompressionSettings(preserve_image=False, quality=85, speed=10))
        with Image.open(result.output_path) as encoded, Image.open(old_output) as old:
            errors = [abs(a - b) for a, b in zip(pixels, encoded.convert("RGB").tobytes())]
            old_errors = [abs(a - b) for a, b in zip(pixels, old.convert("RGB").tobytes())]
            self.assertLess(sum(errors), sum(old_errors) / 20)
            self.assertLessEqual(max(errors), 8)
            self.assertEqual(encoded.size, (64, 32))
            self.assertIsNone(encoded.info.get("icc_profile"))
        from compressor.engine import _avif_properties
        self.assertIn(b"colrnclx" + struct.pack(">HHHB", 1, 13, 0, 128), _avif_properties(result.output_path))
        self.assertEqual(source.read_bytes(), original_file)

    @unittest.skipUnless("AVIF" in available_formats(), "AVIF decoder is not installed")
    def test_lossy_avif_alpha_is_exact_at_low_color_quality(self) -> None:
        source = self.directory / "lossy-alpha.png"
        randomizer = random.Random(84)
        pixels = bytes(randomizer.randrange(256) for _ in range(64 * 16 * 4))
        with Image.frombytes("RGBA", (64, 16), pixels) as image:
            image.save(source)
        result = convert_image(source, CompressionSettings(preserve_image=False, quality=25, speed=10))
        with Image.open(result.output_path) as encoded:
            decoded = encoded.convert("RGBA").tobytes()
            self.assertEqual(decoded[3::4], pixels[3::4])
            self.assertNotEqual(decoded, pixels)

    def test_lossy_avif_encode_failure_removes_all_temporary_and_reserved_files(self) -> None:
        source = self.source_image()
        original = source.read_bytes()
        failure = subprocess.CompletedProcess([], 1, "", "encoder failed")
        with patch("compressor.avif_lossless.subprocess.run", return_value=failure):
            with self.assertRaisesRegex(ValueError, "AVIF 保存に失敗"):
                convert_image(source, CompressionSettings(preserve_image=False))
        self.assertEqual(list(self.directory.iterdir()), [source])
        self.assertEqual(source.read_bytes(), original)

    def test_lossy_avif_does_not_discard_png_gamma_or_conflicting_cicp_and_icc(self) -> None:
        source = self.directory / "lossy-unsupported-color.png"
        png_info = PngInfo()
        png_info.add(b"gAMA", struct.pack(">I", 100000))
        with Image.new("RGB", (8, 4), (47, 190, 71)) as image:
            image.save(source, pnginfo=png_info)
        with self.assertRaisesRegex(ValueError, "ガンマ"):
            convert_image(source, CompressionSettings(preserve_image=False))
        png_info = PngInfo()
        png_info.add(b"cICP", b"\x01\x0d\x00\x01")
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        with Image.new("RGB", (8, 4), (47, 190, 71)) as image:
            image.save(source, pnginfo=png_info, icc_profile=profile)
        for preserve in (True, False):
            with self.subTest(preserve=preserve), self.assertRaisesRegex(ValueError, "CICP と ICC"):
                convert_image(source, CompressionSettings(preserve_image=preserve))
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_lossy_avif_normalizes_exif_orientation_and_resized_dimensions(self) -> None:
        source = self.directory / "lossy-oriented.png"
        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = 6
        exif[ExifTags.Base.Make] = "Test Camera"
        exif[ExifTags.Base.ImageWidth] = 30
        exif[ExifTags.Base.ImageLength] = 20
        with Image.new("RGB", (30, 20), (120, 75, 180)) as image:
            image.save(source, exif=exif)
        result = convert_image(source, CompressionSettings(
            preserve_image=False, quality=85, scale_percent=33, preserve_metadata=True,
        ))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.size, (7, 10))
            encoded_exif = encoded.getexif()
            self.assertNotIn(ExifTags.Base.Orientation, encoded_exif)
            self.assertEqual(encoded_exif[ExifTags.Base.ImageWidth], 7)
            self.assertEqual(encoded_exif[ExifTags.Base.ImageLength], 10)
            self.assertEqual(encoded_exif[ExifTags.Base.Make], "Test Camera")

    def test_lossy_avif_profiled_lab_conversion_moves_pixels_and_profile_together(self) -> None:
        source = self.directory / "lab-profile.tiff"
        lab_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
        srgb_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        with Image.new("LAB", (32, 16), (160, 140, 100)) as image:
            image.save(source, icc_profile=lab_profile.tobytes())
            converted = ImageCms.profileToProfile(image, lab_profile, srgb_profile, outputMode="RGB")
            expected = converted.tobytes()
            converted.close()
        result = convert_image(source, CompressionSettings(preserve_image=False, quality=85))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.mode, "RGB")
            import io
            profile = ImageCms.ImageCmsProfile(io.BytesIO(encoded.info["icc_profile"]))
            self.assertEqual(profile.profile.xcolor_space.strip(), "RGB")
            self.assertLessEqual(max(abs(a - b) for a, b in zip(encoded.tobytes(), expected)), 8)

    def test_lossy_avif_output_verification_rejects_bad_color_signaling_and_cleans_up(self) -> None:
        source = self.source_image()
        original = source.read_bytes()

        def bad_encode(image: Image.Image, destination: Path, **kwargs: object) -> None:
            image.save(destination, format="PNG")

        with patch("compressor.engine.encode_lossy_avif", bad_encode):
            with self.assertRaisesRegex(ValueError, "色空間・色変換"):
                convert_image(source, CompressionSettings(preserve_image=False))
        self.assertEqual(list(self.directory.iterdir()), [source])
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
