"""Exercise actual HEVC codecs, color/alpha preservation and safe overwrites."""

from pathlib import Path
import random
import struct
import tempfile
import unittest
from unittest.mock import patch

from PIL import ExifTags, Image, ImageCms
from PIL.PngImagePlugin import PngInfo

from compressor import CompressionSettings, available_formats, collect_images, convert_image
from compressor.heif import heif_save_options
from compressor.avif_lossless import avif_properties


@unittest.skipUnless("HEIF" in available_formats(), "HEIF codec is not installed")
class HeifTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name)
        cpu = patch("compressor.engine.try_hardware_encode", return_value=None)
        cpu.start()
        self.addCleanup(cpu.stop)

    def save_heif(self, image, path, **options):
        parameters = heif_save_options(quality=85, speed=10, preserve_image=True,
                                      has_alpha=image.mode == "RGBA", icc_profile=None, exif=b"")
        parameters.update(options)
        image.save(path, format="HEIF", **parameters)

    def test_heif_and_heic_are_collected_and_decode_as_input(self):
        paths = [self.directory / "one.heif", self.directory / "two.HEIC"]
        with Image.new("RGB", (32, 24), (73, 146, 219)) as image:
            pixels = image.tobytes()
            for path in paths:
                self.save_heif(image, path)
        self.assertEqual(set(collect_images([self.directory, paths[0]])), set(paths))
        for path in paths:
            with self.subTest(extension=path.suffix):
                result = convert_image(path, CompressionSettings(output_format="PNG"))
                self.assertEqual(result.output_path.name, path.stem + ".png")
                with Image.open(result.output_path) as encoded:
                    self.assertEqual(encoded.tobytes(), pixels)

    def test_lossless_rgb_and_rgba_keep_all_pixels_hidden_colors_and_icc(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        randomizer = random.Random(701)
        for mode, bands in (("RGB", 3), ("RGBA", 4)):
            source = self.directory / (mode + ".png")
            with Image.frombytes(mode, (33, 19), bytes(randomizer.randrange(256) for _ in range(33 * 19 * bands))) as image:
                if mode == "RGBA":
                    for x in range(33):
                        image.putpixel((x, 0), (x * 7, 255 - x, x * 3, 0))
                image.save(source, icc_profile=profile)
                pixels = image.tobytes()
            original = source.read_bytes()
            result = convert_image(source, CompressionSettings(output_format="HEIF", quality=1, scale_percent=1, speed=10))
            self.assertEqual(result.output_path.name, mode + ".heif")
            with Image.open(result.output_path) as encoded:
                self.assertEqual(encoded.format, "HEIF")
                self.assertEqual(encoded.size, (33, 19))
                self.assertEqual(encoded.mode, mode)
                self.assertEqual(encoded.tobytes(), pixels)
                self.assertEqual(encoded.info["icc_profile"], profile)
            self.assertEqual(source.read_bytes(), original)

    def test_grayscale_lossless_stays_grayscale(self):
        source = self.directory / "gray.png"
        pixels = bytes(range(256)) * 3
        with Image.frombytes("L", (32, 24), pixels) as image:
            image.save(source)
        result = convert_image(source, CompressionSettings(output_format="HEIF", speed=10))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.mode, "L")
            self.assertEqual(encoded.tobytes(), pixels)

    def test_nonlossless_rgba_uses_lossless_cpu_encoding_to_keep_alpha_exact(self):
        source = self.directory / "transparent.png"
        randomizer = random.Random(930)
        pixels = bytes(randomizer.randrange(256) for _ in range(32 * 24 * 4))
        with Image.frombytes("RGBA", (32, 24), pixels) as image:
            image.save(source)
        result = convert_image(source, CompressionSettings(output_format="HEIF", preserve_image=False, quality=20, speed=10))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.tobytes(), pixels)

    def test_lossy_rgb_keeps_identity_color_signaling_and_requested_size(self):
        source = self.directory / "rgb.png"
        with Image.new("RGB", (80, 40), (73, 146, 219)) as image:
            image.save(source)
        result = convert_image(source, CompressionSettings(output_format="HEIF", preserve_image=False, quality=85, scale_percent=50, speed=10))
        self.assertEqual(result.backend, "CPU")
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.size, (40, 20))
            self.assertLessEqual(max(abs(a - b) for a, b in zip(encoded.getpixel((20, 10)), (73, 146, 219))), 5)
        self.assertIn(b"colrnclx" + struct.pack(">HHHB", 1, 13, 0, 128), avif_properties(result.output_path))

    def test_metadata_removal_or_preservation_keeps_color_and_normalizes_exif(self):
        source = self.directory / "metadata.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = 6
        exif[ExifTags.Base.Make] = "Camera"
        exif[ExifTags.Base.ImageWidth] = 40
        exif[ExifTags.Base.ImageLength] = 20
        with Image.new("RGB", (40, 20), (73, 146, 219)) as image:
            image.save(source, exif=exif, icc_profile=profile)
        for preserve in (False, True):
            result = convert_image(source, CompressionSettings(output_format="HEIF", preserve_image=False,
                preserve_metadata=preserve, scale_percent=50, speed=10))
            with Image.open(result.output_path) as encoded:
                self.assertEqual(encoded.size, (10, 20))
                self.assertEqual(encoded.info["icc_profile"], profile)
                self.assertNotIn(ExifTags.Base.Orientation, encoded.getexif())
                self.assertFalse(encoded.info.get("xmp"))
                if preserve:
                    self.assertEqual(encoded.getexif()[ExifTags.Base.Make], "Camera")
                    self.assertEqual(encoded.getexif()[ExifTags.Base.ImageWidth], 10)
                    self.assertEqual(encoded.getexif()[ExifTags.Base.ImageLength], 20)
                else:
                    self.assertFalse(encoded.getexif())

    def test_heif_in_place_recompression_and_heic_canonical_output(self):
        source = self.directory / "photo.heif"
        exif = Image.Exif()
        exif[ExifTags.Base.Make] = "Remove me"
        with Image.new("RGB", (32, 24), (20, 100, 200)) as image:
            self.save_heif(image, source, exif=exif.tobytes())
            pixels = image.tobytes()
        original = source.read_bytes()
        result = convert_image(source, CompressionSettings(output_format="HEIF", speed=10, overwrite_original=True))
        self.assertEqual(result.input_path, result.output_path)
        self.assertEqual(result.input_bytes, len(original))
        with Image.open(source) as encoded:
            self.assertEqual(encoded.tobytes(), pixels)
            self.assertFalse(encoded.getexif())
        self.assertNotEqual(source.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [source])
        alias = self.directory / "photo.heic"
        alias.write_bytes(source.read_bytes())
        old_alias = alias.read_bytes()
        result = convert_image(alias, CompressionSettings(output_format="HEIF", speed=10))
        self.assertEqual(result.output_path, source)
        self.assertEqual(alias.read_bytes(), old_alias)

    def test_heif_encoder_failure_preserves_source_and_existing_destination(self):
        source = self.directory / "source.png"
        with Image.new("RGB", (32, 24), "red") as image:
            image.save(source)
        destination = self.directory / "source.heif"
        destination.write_bytes(b"previous output")
        original = source.read_bytes()
        with patch.object(Image.Image, "save", side_effect=ValueError("encoder failed")):
            with self.assertRaisesRegex(ValueError, "encoder failed"):
                convert_image(source, CompressionSettings(output_format="HEIF", overwrite_original=True))
        self.assertEqual(destination.read_bytes(), b"previous output")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(set(self.directory.iterdir()), {source, destination})

    def test_heif_verification_failure_preserves_the_existing_source(self):
        source = self.directory / "source.heif"
        with Image.new("RGB", (32, 24), "red") as image:
            self.save_heif(image, source)
        original = source.read_bytes()
        with patch("compressor.heif.verify_heif_output", side_effect=ValueError("verification failed")):
            with self.assertRaisesRegex(ValueError, "verification failed"):
                convert_image(source, CompressionSettings(output_format="HEIF", overwrite_original=True))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_high_depth_heif_is_rejected_before_pillow_reduction_is_published(self):
        source = self.directory / "hdr.heif"
        with Image.frombytes("I;16", (32, 24), struct.pack("<768H", *range(768))) as image:
            self.save_heif(image, source)
        with Image.open(source) as decoded:
            self.assertEqual(decoded.info["bit_depth"], 10)
        original = source.read_bytes()
        for preserve in (False, True):
            with self.subTest(preserve=preserve), self.assertRaisesRegex(ValueError, "高ビット深度の HEIF"):
                convert_image(source, CompressionSettings(output_format="HEIF", preserve_image=preserve, overwrite_original=True))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_unsupported_heif_color_space_is_rejected_and_existing_files_survive(self):
        source = self.directory / "wide.heif"
        with Image.new("RGB", (32, 24), "red") as image:
            self.save_heif(image, source, color_primaries=9, transfer_characteristics=16)
        original = source.read_bytes()
        with self.assertRaisesRegex(ValueError, "HEIF の色情報"):
            convert_image(source, CompressionSettings(output_format="PNG"))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_multiple_heif_images_are_rejected(self):
        source = self.directory / "sequence.heif"
        with Image.new("RGB", (32, 24), "red") as first, Image.new("RGB", (32, 24), "blue") as second:
            self.save_heif(first, source, save_all=True, append_images=[second])
        original = source.read_bytes()
        with self.assertRaisesRegex(ValueError, "複数フレーム"):
            convert_image(source, CompressionSettings(output_format="PNG"))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [source])

    def test_png_gamma_is_not_discarded_in_heif(self):
        source = self.directory / "gamma.png"
        chunks = PngInfo()
        chunks.add(b"gAMA", struct.pack(">I", 100000))
        with Image.new("RGB", (32, 24), "red") as image:
            image.save(source, pnginfo=chunks)
        for preserve in (False, True):
            with self.subTest(preserve=preserve), self.assertRaisesRegex(ValueError, "ガンマ"):
                convert_image(source, CompressionSettings(output_format="HEIF", preserve_image=preserve))
        self.assertEqual(list(self.directory.iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
