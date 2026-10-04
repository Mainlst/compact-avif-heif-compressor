"""HEVC still-image muxing boundaries and an optional actual GPU roundtrip."""

from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pillow_heif

from compressor import hardware
from compressor.heif_container import heif_properties, mux_heif


pillow_heif.register_heif_opener()


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def one_frame_mp4(
    *, frame_count: int = 1, offset: int | None = None, matrix: int = 1,
    bit_depth: int = 8, nal_type: int = 19,
) -> bytes:
    """Minimal structural fixture; parameter NALs intentionally contain no pixels."""
    configuration = bytearray(23)
    configuration[0] = 1
    configuration[1] = 1
    configuration[16] = 1
    configuration[17] = configuration[18] = bit_depth - 8
    configuration[21] = 3
    configuration[22] = 3
    for kind in (32, 33, 34):
        configuration.extend(bytes((0x80 | kind,)) + struct.pack(">HH", 1, 2) + bytes((kind << 1, 1)))
    frame = struct.pack(">I", 3) + bytes((nal_type << 1, 1, 0x80))
    ftyp = box(b"ftyp", b"isom" + b"\0" * 4 + b"isom")
    mdat = box(b"mdat", frame)
    entry = bytearray(78)
    struct.pack_into(">HH", entry, 24, 256, 128)
    sample_entry = box(b"hvc1", bytes(entry) + box(b"hvcC", bytes(configuration)) + box(
        b"colr", b"nclx" + struct.pack(">HHHB", 1, 13, matrix, 128)
    ))
    table = box(b"stsd", b"\0" * 4 + struct.pack(">I", 1) + sample_entry)
    table += box(b"stsz", b"\0" * 4 + struct.pack(">II", len(frame), frame_count))
    table += box(b"stco", b"\0" * 4 + struct.pack(">II", 1, len(ftyp) + 8 if offset is None else offset))
    moov = box(b"moov", box(b"trak", box(b"mdia", box(b"minf", box(b"stbl", table)))))
    return ftyp + mdat + moov


class HeifHardwareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name)
        hardware._probe_encoder.cache_clear()
        self.addCleanup(hardware._probe_encoder.cache_clear)
        self.encoder = hardware.HardwareEncoder("hevc_nvenc", "HEIF", "GPU · NVIDIA NVENC（4:2:0）")

    def test_mux_retains_hevc_sample_and_signals_one_eight_bit_image(self) -> None:
        source = self.directory / "input.mp4"
        destination = self.directory / "output.heic"
        original = one_frame_mp4()
        source.write_bytes(original)
        mux_heif(source, destination, (256, 128))
        properties = heif_properties(destination)
        self.assertIn(b"colrnclx" + struct.pack(">HHHB", 1, 13, 1, 128), properties)
        self.assertIn(b"ispe\0\0\0\0" + struct.pack(">II", 256, 128), properties)
        self.assertIn(b"pixi\0\0\0\0\x03\x08\x08\x08", properties)
        data = destination.read_bytes()
        self.assertEqual(data[8:12], b"heic")
        self.assertTrue(data.endswith(struct.pack(">I", 3) + b"\x26\x01\x80"))
        self.assertNotIn(b"moov", data)
        self.assertEqual(source.read_bytes(), original)

    def test_mux_rejects_multiple_frames_wrong_size_color_depth_and_invalid_sample_offsets(self) -> None:
        source = self.directory / "input.mp4"
        destination = self.directory / "output.heic"
        for options, expected_size in (
            ({"frame_count": 2}, (256, 128)), ({"offset": 0xFFFFFF00}, (256, 128)),
            ({"matrix": 6}, (256, 128)), ({"bit_depth": 10}, (256, 128)),
            ({"nal_type": 1}, (256, 128)), ({}, (128, 256)),
        ):
            with self.subTest(options=options, size=expected_size):
                source.write_bytes(one_frame_mp4(**options))
                with self.assertRaises(ValueError):
                    mux_heif(source, destination, expected_size)
                self.assertFalse(destination.exists())

    def test_mux_rejects_truncated_bmff_without_publishing_a_partial_file(self) -> None:
        source = self.directory / "input.mp4"
        destination = self.directory / "output.heic"
        data = one_frame_mp4()
        for truncated in (b"", data[:6], data[:-1], struct.pack(">I4s", 0xFFFFFFFF, b"moov") + data):
            with self.subTest(length=len(truncated)):
                source.write_bytes(truncated)
                with self.assertRaises(ValueError):
                    mux_heif(source, destination, (256, 128))
                self.assertFalse(destination.exists())

    def test_heif_hardware_command_is_one_hvc1_frame_with_explicit_color_and_no_b_frames(self) -> None:
        command = hardware._command(Path("ffmpeg"), self.encoder, Path("input.png"), Path("output.mp4"), 85, 6)
        for flag, value in (("-frames:v", "1"), ("-tag:v", "hvc1"), ("-g", "2"), ("-bf", "0"), ("-f", "mp4")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("hevc_nvenc", command)
        self.assertIn("setparams=range=full:color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709", command[command.index("-vf") + 1])

    def test_heif_profiles_exif_grayscale_and_alpha_use_cpu_without_probing(self) -> None:
        with patch.object(hardware, "_ffmpeg_path") as discover:
            for mode, profile, exif in (("L", None, b""), ("RGBA", None, b""), ("RGB", b"icc", b""), ("RGB", None, b"Exif\0\0")):
                with self.subTest(mode=mode, profile=profile), Image.new(mode, (256, 128)) as image:
                    self.assertIsNone(hardware.try_hardware_encode(
                        image, self.directory / "output.heic", output_format="HEIF", quality=85, icc_profile=profile, exif=exif,
                    ))
            discover.assert_not_called()

    def test_invalid_hevc_mux_output_returns_to_cpu_and_removes_temporary_files(self) -> None:
        def corrupt_mp4(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            Path(arguments[-1]).write_bytes(b"broken MP4")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        destination = self.directory / "output.heic"
        with Image.new("RGB", (256, 128)) as image, patch.object(
            hardware, "_ffmpeg_path", return_value=Path("ffmpeg")
        ), patch.object(hardware, "_installed_encoders", return_value=frozenset({"hevc_nvenc"})), patch.object(
            hardware, "_candidates", return_value=(self.encoder,)
        ), patch.object(hardware, "_probe_encoder", return_value=True), patch.object(hardware, "_run", side_effect=corrupt_mp4):
            self.assertIsNone(hardware.try_hardware_encode(image, destination, output_format="HEIF", quality=85))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_actual_nvenc_heif_roundtrip_when_runtime_supports_hevc(self) -> None:
        binary = hardware._ffmpeg_path()
        if binary is None or "hevc_nvenc" not in hardware._installed_encoders(binary) or not hardware._probe_encoder(binary, self.encoder):
            self.skipTest("A working NVENC HEVC device is required for this actual GPU integration test")
        destination = self.directory / "actual-gpu.heic"
        with Image.new("RGB", (288, 160), (73, 146, 219)) as image:
            pixels = image.tobytes()
            label = hardware.try_hardware_encode(image, destination, output_format="HEIF", quality=85)
            self.assertEqual(image.tobytes(), pixels)
        self.assertEqual(label, "GPU · NVIDIA NVENC（4:2:0）")
        with Image.open(destination) as encoded:
            encoded.load()
            self.assertEqual(encoded.format, "HEIF")
            self.assertEqual(encoded.size, (288, 160))
            self.assertEqual(encoded.mode, "RGB")
            self.assertFalse(encoded.getexif())
            self.assertNotIn("icc_profile", encoded.info)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(encoded.getpixel((144, 80)), (73, 146, 219))), 5)
        self.assertFalse(list(self.directory.glob(".image-compressor*")))


if __name__ == "__main__":
    unittest.main()
