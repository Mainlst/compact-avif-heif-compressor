"""Use hardware image encoders only after a real, verified encode succeeds.

FFmpeg advertising an encoder does not imply that a compatible GPU or driver
is present. Probes and encoding run on the conversion worker, never at import
time. Lossless formats stay on the established, pixel-verified CPU path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

from PIL import Image

from .avif_lossless import avif_properties
from .heif_container import heif_properties, mux_heif


@dataclass(frozen=True)
class HardwareEncoder:
    name: str
    output_format: str
    label: str
    device: str | None = None


@lru_cache(maxsize=1)
def _ffmpeg_path() -> Path | None:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    platform = "windows" if sys.platform == "win32" else "linux"
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates = [root / "vendor" / "ffmpeg" / platform / name]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    installed = shutil.which(name)
    return Path(installed).resolve() if installed else None


def _run(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


@lru_cache(maxsize=2)
def _installed_encoders(ffmpeg: Path) -> frozenset[str]:
    try:
        result = _run([str(ffmpeg), "-hide_banner", "-encoders"], 10)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode:
        return frozenset()
    return frozenset(re.findall(r"^\s*V\S{5}\s+([\w-]+)\s", result.stdout, re.MULTILINE))


def _candidates(output_format: str) -> tuple[HardwareEncoder, ...]:
    if output_format == "JPEG":
        encoders = [HardwareEncoder("mjpeg_qsv", "JPEG", "GPU · Intel QSV")]
    elif output_format in {"AVIF", "HEIF"}:
        codec = "av1" if output_format == "AVIF" else "hevc"
        encoders = [
            HardwareEncoder(f"{codec}_nvenc", output_format, "GPU · NVIDIA NVENC（4:2:0）"),
            HardwareEncoder(f"{codec}_qsv", output_format, "GPU · Intel QSV（4:2:0）"),
            HardwareEncoder(f"{codec}_amf", output_format, "GPU · AMD AMF（4:2:0）"),
        ]
    else:
        return ()
    if sys.platform.startswith("linux"):
        # Each render node can belong to a different GPU. Probe them separately.
        for device in sorted(Path("/dev/dri").glob("renderD*")):
            name = {"JPEG": "mjpeg_vaapi", "AVIF": "av1_vaapi", "HEIF": "hevc_vaapi"}[output_format]
            label = "GPU · VAAPI" + ("（4:2:0）" if output_format in {"AVIF", "HEIF"} else "")
            encoders.append(HardwareEncoder(name, output_format, label, str(device)))
    return tuple(encoders)


def _command(
    ffmpeg: Path, encoder: HardwareEncoder, source: Path, destination: Path,
    quality: int, speed: int,
) -> list[str]:
    arguments = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if encoder.name.endswith("_qsv"):
        # Explicitly request hardware: QSV's default auto_any may select software.
        arguments.extend(("-init_hw_device", "qsv=compressor:hw", "-filter_hw_device", "compressor"))
    elif encoder.name.endswith("_vaapi"):
        arguments.extend(("-vaapi_device", str(encoder.device)))
    arguments.extend(("-i", str(source), "-map", "0:v:0", "-map_metadata", "-1", "-frames:v", "1", "-an"))
    if encoder.output_format == "JPEG":
        # JPEG uses full-range BT.601 YCbCr, independently of the attached ICC.
        filters = "scale=in_range=pc:out_range=pc:out_color_matrix=bt601,format=nv12"
        if encoder.name.endswith("_vaapi"):
            filters += ",hwupload"
        arguments.extend((
            "-vf", filters, "-c:v", encoder.name,
            "-global_quality", str(max(1, quality)), "-color_range", "pc",
        ))
        if encoder.name.endswith("_vaapi"):
            arguments.extend(("-jfif", "1"))
        arguments.extend(("-f", "image2", "-update", "1"))
    else:
        # Hardware AV1/HEVC uses 4:2:0. Label and verify its color conversion
        # explicitly; the CPU path retains its existing 4:4:4 identity encoding.
        pixel_format = "yuv420p" if encoder.name.endswith("_nvenc") else "nv12"
        filters = f"scale=in_range=pc:out_range=pc:out_color_matrix=bt709,format={pixel_format}"
        # Newer FFmpeg encoders inherit frame color properties, which override
        # CLI color flags when an untagged PNG reports unspecified primaries.
        filters += ",setparams=range=full:color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709"
        if encoder.name.endswith("_vaapi"):
            filters += ",hwupload"
        arguments.extend((
            "-vf", filters, "-c:v", encoder.name,
            "-pix_fmt", "+vaapi" if encoder.name.endswith("_vaapi") else f"+{pixel_format}",
            "-color_range", "pc", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "iec61966-2-1",
        ))
        quantizer = max(1, round((100 - quality) * 51 / 100))
        if encoder.name.endswith("_nvenc"):
            preset = f"p{max(1, min(7, 7 - round(speed * 6 / 10)))}"
            arguments.extend(("-preset", preset, "-rc", "constqp", "-qp", str(quantizer)))
        elif encoder.name.endswith("_amf"):
            arguments.extend(("-rc", "cqp", "-qp_i", str(quantizer)))
        else:
            arguments.extend(("-global_quality", str(quantizer)))
        if encoder.output_format == "HEIF":
            # NVENC requires GOP length > B-frame count + 1; only the first
            # independently decodable intra frame is stored in this MP4.
            arguments.extend(("-g", "2", "-bf", "0", "-tag:v", "hvc1", "-f", "mp4"))
        else:
            arguments.extend(("-f", "avif"))
    arguments.append(str(destination))
    return arguments


def _verify_output(path: Path, expected: Image.Image, output_format: str) -> None:
    with Image.open(path) as encoded:
        if encoded.format != output_format or encoded.size != expected.size:
            raise ValueError("ハードウェアエンコードの形式・画像サイズが一致しません。")
        encoded.load()
    if output_format == "AVIF":
        properties = avif_properties(path)
        nclx = b"colrnclx" + struct.pack(">HHHB", 1, 13, 1, 0x80)
        configs = [item for item in properties if item[:4] == b"av1C"]
        if nclx not in properties or not configs or any(
            len(item) < 8 or item[6] & 0x7C != 0x0C for item in configs
        ):
            raise ValueError("GPU が AVIF の sRGB・BT.709・4:2:0・フルレンジを保存できません。")
    elif output_format == "HEIF":
        properties = heif_properties(path)
        nclx = b"colrnclx" + struct.pack(">HHHB", 1, 13, 1, 0x80)
        configurations = [item[4:] for item in properties if item[:4] == b"hvcC"]
        if nclx not in properties or len(configurations) != 1:
            raise ValueError("GPU の HEIF 色情報を検証できませんでした。")
        configuration = configurations[0]
        if len(configuration) < 23 or configuration[16] & 3 != 1 or configuration[17] & 7 or configuration[18] & 7:
            raise ValueError("GPU が HEIF の 8 ビット・4:2:0 を保存できません。")


def _encode(
    ffmpeg: Path, encoder: HardwareEncoder, image: Image.Image, destination: Path,
    quality: int, speed: int, timeout: int,
) -> None:
    with tempfile.TemporaryDirectory(prefix=".image-compressor-gpu-", dir=destination.parent) as workspace:
        source = Path(workspace) / "input.png"
        image.save(source, format="PNG", compress_level=0, icc_profile=None, exif=b"")
        encoded_path = Path(workspace) / "encoded.mp4" if encoder.output_format == "HEIF" else destination
        result = _run(_command(ffmpeg, encoder, source, encoded_path, quality, speed), timeout)
        if result.returncode:
            raise ValueError("ハードウェアエンコーダーを実行できません。")
        if encoder.output_format == "HEIF":
            mux_heif(encoded_path, destination, image.size)
        _verify_output(destination, image, encoder.output_format)


@lru_cache(maxsize=32)
def _probe_encoder(ffmpeg: Path, encoder: HardwareEncoder) -> bool:
    """A driver must actually encode, and decoded colors must be plausible."""
    try:
        with tempfile.TemporaryDirectory(prefix="image-compressor-gpu-probe-") as workspace:
            output = Path(workspace) / {"JPEG": "probe.jpg", "AVIF": "probe.avif", "HEIF": "probe.heic"}[encoder.output_format]
            with Image.new("RGB", (256, 256)) as sample:
                colors = ((0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255), (73, 146, 219))
                for index, color in enumerate(colors):
                    sample.paste(color, (index * 40, 0, (index + 1) * 40, 256))
                _encode(ffmpeg, encoder, sample, output, 95, 10, 15)
                with Image.open(output) as encoded, encoded.convert("RGB") as rgb:
                    for index, color in enumerate(colors):
                        if max(abs(actual - expected) for actual, expected in zip(rgb.getpixel((index * 40 + 20, 128)), color)) > 12:
                            return False
            return True
    except (OSError, ValueError, Image.DecompressionBombError, subprocess.SubprocessError):
        return False


def _attach_jpeg_metadata(path: Path, profile: bytes | None, exif: bytes) -> None:
    """Insert ICC/EXIF APP markers without decoding or recompressing the JPEG."""
    markers = bytearray()

    def add_marker(kind: int, payload: bytes) -> None:
        if len(payload) > 65533:
            raise ValueError("JPEG のメタデータが大きすぎます。")
        markers.extend(bytes((255, kind)) + struct.pack(">H", len(payload) + 2) + payload)

    if exif:
        add_marker(0xE1, exif if exif.startswith(b"Exif\0\0") else b"Exif\0\0" + exif)
    if profile:
        chunk_size = 65519
        chunks = [profile[index:index + chunk_size] for index in range(0, len(profile), chunk_size)]
        if len(chunks) > 255:
            raise ValueError("JPEG のカラープロファイルが大きすぎます。")
        for index, chunk in enumerate(chunks, 1):
            add_marker(0xE2, b"ICC_PROFILE\0" + bytes((index, len(chunks))) + chunk)

    original = path.read_bytes()
    if original[:2] != b"\xff\xd8":
        raise ValueError("JPEG の保存結果が不正です。")
    result = bytearray(original[:2])
    position = 2
    inserted = False
    while position + 4 <= len(original):
        if original[position] != 255:
            raise ValueError("JPEG のヘッダーが不正です。")
        kind = original[position + 1]
        if kind != 0xE0 and not inserted:
            result.extend(markers)
            inserted = True
        if kind == 0xDA:  # Preserve the entropy-coded scan verbatim.
            result.extend(original[position:])
            path.write_bytes(result)
            return
        length = struct.unpack(">H", original[position + 2:position + 4])[0]
        end = position + 2 + length
        if length < 2 or end > len(original):
            raise ValueError("JPEG のヘッダーが不正です。")
        # Never carry EXIF/XMP/ICC/comments supplied by a codec into the result.
        if kind not in {0xE1, 0xE2, 0xFE}:
            result.extend(original[position:end])
        position = end
    raise ValueError("JPEG の画像データがありません。")


def try_hardware_encode(
    image: Image.Image, destination: Path, *, output_format: str, quality: int,
    speed: int = 6, icc_profile: bytes | None = None, exif: bytes = b"",
    verify_output: Callable[[Path], None] | None = None,
) -> str | None:
    """Return a verified GPU backend label, or leave CPU fallback to the caller.

    AVIF/HEIF hardware is eligible only for unprofiled opaque RGB. FFmpeg cannot
    reliably mux the original ICC/EXIF or lossless alpha into this path. Those
    images, grayscale and all lossless settings use their CPU codecs unchanged.
    """
    if image.mode != "RGB" or output_format not in {"AVIF", "HEIF", "JPEG"}:
        return None
    if output_format in {"AVIF", "HEIF"} and (icc_profile or exif):
        return None
    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        return None
    installed = _installed_encoders(ffmpeg)
    for encoder in _candidates(output_format):
        if encoder.name not in installed or not _probe_encoder(ffmpeg, encoder):
            continue
        try:
            _encode(ffmpeg, encoder, image, destination, quality, speed, 90)
            if output_format == "JPEG":
                _attach_jpeg_metadata(destination, icc_profile, exif)
                with Image.open(destination) as encoded:
                    encoded.load()
                    if encoded.info.get("icc_profile") != icc_profile or encoded.info.get("exif", b"") != exif:
                        raise ValueError("GPU 保存で ICC・EXIF を保持できません。")
            if verify_output is not None:
                verify_output(destination)
            return encoder.label
        except (OSError, ValueError, Image.DecompressionBombError, subprocess.SubprocessError):
            # Includes device loss, unsupported dimensions and failed validation.
            destination.unlink(missing_ok=True)
    return None
