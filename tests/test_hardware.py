"""Hardware discovery, real-encode probes, metadata and CPU fallback contracts."""

from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import ExifTags, Image, ImageCms

from compressor import CompressionSettings, convert_image
from compressor import hardware


class HardwareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name)
        hardware._probe_encoder.cache_clear()
        hardware._installed_encoders.cache_clear()
        self.addCleanup(hardware._probe_encoder.cache_clear)
        self.addCleanup(hardware._installed_encoders.cache_clear)
        self.encoder = hardware.HardwareEncoder("mjpeg_qsv", "JPEG", "GPU · Intel QSV")
        self.binary = self.directory / "ffmpeg"

    def simulate_jpeg_encode(self, arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        # Stand in for the hardware process while exercising actual PNG input,
        # JPEG decoding, color probes and metadata injection without a GPU.
        source = arguments[arguments.index("-i") + 1]
        destination = arguments[-1]
        quality = int(arguments[arguments.index("-global_quality") + 1])
        with Image.open(source) as image:
            self.assertNotIn("exif", image.info)
            self.assertNotIn("icc_profile", image.info)
            image.save(destination, format="JPEG", quality=quality)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def test_encoder_listing_alone_does_not_enable_a_gpu(self) -> None:
        rejected = subprocess.CompletedProcess([], 1, "", "device unavailable")
        with patch.object(hardware, "_run", return_value=rejected) as run:
            self.assertFalse(hardware._probe_encoder(self.binary, self.encoder))
            self.assertFalse(hardware._probe_encoder(self.binary, self.encoder))
        self.assertEqual(run.call_count, 1, "Failed runtime probes should be cached for this process")

    def test_probe_decodes_a_real_encoded_image_and_checks_its_colors(self) -> None:
        with patch.object(hardware, "_run", side_effect=self.simulate_jpeg_encode):
            self.assertTrue(hardware._probe_encoder(self.binary, self.encoder))

        hardware._probe_encoder.cache_clear()

        def incorrect_colors(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            with Image.new("RGB", (256, 256), "black") as image:
                image.save(arguments[-1], format="JPEG")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.object(hardware, "_run", side_effect=incorrect_colors):
            self.assertFalse(hardware._probe_encoder(self.binary, self.encoder))

    def test_qsv_command_requests_hardware_and_keeps_jpeg_full_range(self) -> None:
        command = hardware._command(self.binary, self.encoder, Path("input.png"), Path("output.tmp"), 85, 6)
        self.assertIn("qsv=compressor:hw", command)
        self.assertIn("scale=in_range=pc:out_range=pc:out_color_matrix=bt601,format=nv12", command)
        self.assertEqual(command[command.index("-global_quality") + 1], "85")

    def test_avif_command_explicitly_converts_and_labels_full_range_bt709_420(self) -> None:
        encoder = hardware.HardwareEncoder("av1_nvenc", "AVIF", "GPU · NVIDIA NVENC")
        command = hardware._command(self.binary, encoder, Path("input.png"), Path("output.tmp"), 85, 6)
        self.assertEqual(command[command.index("-pix_fmt") + 1], "+yuv420p")
        self.assertEqual(command[command.index("-colorspace") + 1], "bt709")
        self.assertEqual(command[command.index("-color_range") + 1], "pc")
        self.assertEqual(command[command.index("-color_trc") + 1], "iec61966-2-1")
        filters = command[command.index("-vf") + 1]
        self.assertIn("scale=in_range=pc:out_range=pc:out_color_matrix=bt709,format=yuv420p", filters)
        self.assertIn("setparams=range=full:color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709", filters)

    def test_avif_with_an_incorrect_color_matrix_fails_runtime_probe(self) -> None:
        encoder = hardware.HardwareEncoder("av1_nvenc", "AVIF", "GPU · NVIDIA NVENC")

        def subsampled_avif(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            source = arguments[arguments.index("-i") + 1]
            with Image.open(source) as image:
                image.save(arguments[-1], format="AVIF", speed=10, quality=95, max_threads=1)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.object(hardware, "_run", side_effect=subsampled_avif):
            self.assertFalse(hardware._probe_encoder(self.binary, encoder))

    def test_avif_gpu_route_accepts_actual_full_range_bt709_420_output(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or "libaom-av1" not in hardware._installed_encoders(Path(ffmpeg)):
            self.skipTest("This device-independent AVIF muxing test requires FFmpeg's optional libaom AV1 encoder")
        source = self.directory / "source.png"
        with Image.new("RGB", (64, 32), (73, 146, 219)) as image:
            image.save(source)
        original = source.read_bytes()
        encoder = hardware.HardwareEncoder("av1_nvenc", "AVIF", "GPU · NVIDIA NVENC（4:2:0）")

        def encode_with_real_avif_muxer(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            # The actual FFmpeg AVIF muxer and colorspace filters are exercised;
            # substitute its CPU AV1 encoder where CI lacks an NVENC device.
            rewritten = list(arguments)
            rewritten[0] = ffmpeg
            rewritten[rewritten.index("-c:v") + 1] = "libaom-av1"
            for option in ("-preset", "-rc", "-qp"):
                index = rewritten.index(option)
                del rewritten[index:index + 2]
            rewritten[-1:-1] = ["-cpu-used", "8", "-crf", "5", "-still-picture", "1"]
            return subprocess.run(rewritten, capture_output=True, text=True, check=False, timeout=timeout)

        with patch.object(hardware, "_ffmpeg_path", return_value=self.binary), patch.object(
            hardware, "_installed_encoders", return_value=frozenset({"av1_nvenc"})
        ), patch.object(hardware, "_candidates", return_value=(encoder,)), patch.object(
            hardware, "_run", side_effect=encode_with_real_avif_muxer
        ):
            result = convert_image(source, CompressionSettings(preserve_image=False, quality=85))
        self.assertTrue(result.uses_gpu)
        self.assertEqual(result.backend, "GPU · NVIDIA NVENC（4:2:0）")
        from compressor.avif_lossless import avif_properties
        self.assertIn(b"colrnclx" + struct.pack(">HHHB", 1, 13, 1, 128), avif_properties(result.output_path))
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.format, "AVIF")
            self.assertEqual(encoded.size, (64, 32))
            self.assertNotIn("icc_profile", encoded.info)
            self.assertFalse(encoded.getexif())
            self.assertLessEqual(max(abs(actual - expected) for actual, expected in zip(encoded.getpixel((32, 16)), (73, 146, 219))), 5)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(self.directory.glob(".image-compressor*")))

    def test_invalid_gpu_avif_color_signaling_falls_back_to_cpu_identity_output(self) -> None:
        source = self.directory / "source.png"
        with Image.new("RGB", (64, 32), (73, 146, 219)) as image:
            image.save(source)
        encoder = hardware.HardwareEncoder("av1_nvenc", "AVIF", "GPU · NVIDIA NVENC（4:2:0）")

        def wrong_colorspace(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            with Image.open(arguments[arguments.index("-i") + 1]) as image:
                image.save(arguments[-1], format="AVIF", speed=10, quality=85, max_threads=1)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.object(hardware, "_ffmpeg_path", return_value=self.binary), patch.object(
            hardware, "_installed_encoders", return_value=frozenset({"av1_nvenc"})
        ), patch.object(hardware, "_candidates", return_value=(encoder,)), patch.object(
            hardware, "_probe_encoder", return_value=True
        ), patch.object(hardware, "_run", side_effect=wrong_colorspace):
            result = convert_image(source, CompressionSettings(preserve_image=False, quality=85))
        self.assertEqual(result.backend, "CPU")
        from compressor.avif_lossless import avif_properties
        self.assertIn(b"colrnclx" + struct.pack(">HHHB", 1, 13, 0, 128), avif_properties(result.output_path))
        self.assertFalse(list(self.directory.glob(".image-compressor*")))

    def test_ineligible_modes_and_profiled_avif_do_not_probe_a_gpu(self) -> None:
        with patch.object(hardware, "_ffmpeg_path") as discover:
            for mode, output_format, profile, exif in (
                ("RGBA", "AVIF", None, b""), ("L", "JPEG", None, b""),
                ("RGB", "PNG", None, b""), ("RGB", "WEBP", None, b""),
                ("RGB", "AVIF", b"profile", b""), ("RGB", "AVIF", None, b"Exif\0\0"),
            ):
                with self.subTest(mode=mode, output_format=output_format), Image.new(mode, (16, 16)) as image:
                    self.assertIsNone(hardware.try_hardware_encode(
                        image, self.directory / "output.tmp", output_format=output_format,
                        quality=85, icc_profile=profile, exif=exif,
                    ))
            discover.assert_not_called()

    def test_gpu_jpeg_keeps_icc_and_normalized_exif_without_recompression(self) -> None:
        source = self.directory / "source.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = 6
        exif[ExifTags.Base.Make] = "Test camera"
        exif[ExifTags.Base.ImageWidth] = 40
        exif[ExifTags.Base.ImageLength] = 20
        with Image.new("RGB", (40, 20), (73, 146, 219)) as image:
            image.save(source, icc_profile=profile, exif=exif)
        original = source.read_bytes()
        with patch.object(hardware, "_ffmpeg_path", return_value=self.binary), patch.object(
            hardware, "_installed_encoders", return_value=frozenset({"mjpeg_qsv"})
        ), patch.object(hardware, "_candidates", return_value=(self.encoder,)), patch.object(
            hardware, "_run", side_effect=self.simulate_jpeg_encode
        ):
            for retain in (False, True):
                with self.subTest(preserve_metadata=retain):
                    result = convert_image(source, CompressionSettings(
                        output_format="JPEG", quality=85, preserve_image=False, preserve_metadata=retain,
                    ))
                    self.assertTrue(result.uses_gpu)
                    self.assertEqual(result.backend, "GPU · Intel QSV")
                    with Image.open(result.output_path) as encoded:
                        self.assertEqual(encoded.size, (20, 40))
                        self.assertEqual(encoded.info["icc_profile"], profile)
                        self.assertNotIn(ExifTags.Base.Orientation, encoded.getexif())
                        if retain:
                            self.assertEqual(encoded.getexif()[ExifTags.Base.Make], "Test camera")
                            self.assertEqual(encoded.getexif()[ExifTags.Base.ImageWidth], 20)
                            self.assertEqual(encoded.getexif()[ExifTags.Base.ImageLength], 40)
                        else:
                            self.assertFalse(encoded.getexif())
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(self.directory.glob(".image-compressor*")))

    def test_device_loss_after_a_successful_probe_falls_back_to_cpu(self) -> None:
        source = self.directory / "source.png"
        with Image.new("RGB", (17, 19), "red") as image:
            image.save(source)

        def device_lost(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            Path(arguments[-1]).write_bytes(b"partial hardware output")
            raise subprocess.TimeoutExpired(arguments, timeout)

        with patch.object(hardware, "_ffmpeg_path", return_value=self.binary), patch.object(
            hardware, "_installed_encoders", return_value=frozenset({"mjpeg_qsv"})
        ), patch.object(hardware, "_candidates", return_value=(self.encoder,)), patch.object(
            hardware, "_probe_encoder", return_value=True
        ), patch.object(hardware, "_run", side_effect=device_lost):
            result = convert_image(source, CompressionSettings(output_format="JPEG", preserve_image=False))
        self.assertEqual(result.backend, "CPU")
        self.assertFalse(result.uses_gpu)
        with Image.open(result.output_path) as encoded:
            self.assertEqual(encoded.format, "JPEG")
            self.assertEqual(encoded.size, (17, 19))
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["source.jpg", "source.png"])

    def test_jpeg_app_marker_update_keeps_pixels_and_chunks_large_profiles(self) -> None:
        path = self.directory / "encoded.jpg"
        old_exif = Image.Exif()
        old_exif[ExifTags.Base.Make] = "Remove me"
        with Image.new("RGB", (16, 16), (73, 146, 219)) as image:
            image.save(path, exif=old_exif, icc_profile=b"old profile")
        with Image.open(path) as encoded:
            pixels = encoded.tobytes()
        profile = bytes(range(256)) * 512
        hardware._attach_jpeg_metadata(path, profile, b"")
        with Image.open(path) as encoded:
            self.assertEqual(encoded.tobytes(), pixels)
            self.assertEqual(encoded.info["icc_profile"], profile)
            self.assertFalse(encoded.getexif())
        hardware._attach_jpeg_metadata(path, None, b"")
        with Image.open(path) as encoded:
            self.assertEqual(encoded.tobytes(), pixels)
            self.assertNotIn("icc_profile", encoded.info)

    def test_lossless_conversion_never_calls_hardware(self) -> None:
        source = self.directory / "source.png"
        with Image.new("RGBA", (16, 16), (73, 146, 219, 0)) as image:
            image.save(source)
        with patch("compressor.engine.try_hardware_encode") as gpu:
            for output_format in ("AVIF", "PNG", "WEBP"):
                with self.subTest(output_format=output_format):
                    result = convert_image(
                        source, CompressionSettings(output_format=output_format), self.directory / "output",
                    )
                    self.assertEqual(result.backend, "CPU")
            gpu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
