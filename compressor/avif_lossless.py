"""Encode AVIF with the bundled, official libavif encoder.

Pillow's AVIF encoder always converts RGB through a BT.601 YUV matrix. Even
quality 100 and 4:4:4 therefore change RGB values. An identity matrix avoids
this conversion for both lossy and lossless RGB encoding.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile

from PIL import Image


def avif_properties(source: Path) -> tuple[bytes, ...]:
    """Read properties hidden by Pillow, including source bit depth and CICP."""
    properties: list[bytes] = []
    with source.open("rb") as stream:
        size = source.stat().st_size

        def read_boxes(start: int, end: int) -> None:
            position = start
            while position + 8 <= end:
                stream.seek(position)
                length, kind = struct.unpack(">I4s", stream.read(8))
                header_size = 8
                if length == 1:
                    if position + 16 > end:
                        return
                    length = struct.unpack(">Q", stream.read(8))[0]
                    header_size = 16
                elif length == 0:
                    length = end - position
                if length < header_size or position + length > end:
                    return
                data_start, data_end = position + header_size, position + length
                if kind in {b"meta", b"iprp", b"ipco"}:
                    read_boxes(data_start + (4 if kind == b"meta" else 0), data_end)
                elif kind in {b"pixi", b"colr", b"av1C"} and data_end - data_start <= 32:
                    stream.seek(data_start)
                    properties.append(kind + stream.read(data_end - data_start))
                position += length

        # An AVIF is an ISO BMFF container; other inputs must not be parsed as boxes.
        if stream.read(12)[4:8] == b"ftyp":
            read_boxes(0, size)
    return tuple(properties)


def _encoder_path() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    if sys.platform == "win32":
        binary = root / "vendor" / "libavif" / "windows" / "avifenc.exe"
    elif sys.platform.startswith("linux"):
        binary = root / "vendor" / "libavif" / "linux" / "avifenc"
    else:
        raise ValueError("この環境では AVIF 保存機能が利用できません。")
    if not binary.is_file():
        raise ValueError("AVIF エンコーダーが見つかりません。")
    return binary


def encode_lossless_avif(
    image: Image.Image,
    destination: Path,
    speed: int = 6,
    *,
    icc_profile: bytes | None = None,
    exif: bytes = b"",
) -> None:
    """Write a lossless AVIF, retaining supplied ICC and normalized EXIF."""
    _encode_avif(image, destination, speed, None, icc_profile=icc_profile, exif=exif)


def encode_lossy_avif(
    image: Image.Image,
    destination: Path,
    quality: int = 65,
    speed: int = 6,
    *,
    icc_profile: bytes | None = None,
    exif: bytes = b"",
) -> None:
    """Compress color without subsampling or RGB-to-YUV matrix conversion.

    RGB channels still undergo lossy quantization; alpha stays lossless. An
    existing ICC defines the color space. Untagged SDR inputs are labeled sRGB.
    """
    if type(quality) is not int or not 0 <= quality <= 100:
        raise ValueError("品質は 0 ～ 100 の整数で指定してください。")
    _encode_avif(image, destination, speed, quality, icc_profile=icc_profile, exif=exif)


def _encode_avif(
    image: Image.Image,
    destination: Path,
    speed: int,
    quality: int | None,
    *,
    icc_profile: bytes | None,
    exif: bytes,
) -> None:
    """Share safe temporary input creation, color signaling and process handling.

    An intermediate PNG keeps the decoded source pixels unchanged, including
    RGB values under fully transparent pixels. The caller verifies the output
    before publishing it and handles removal of a partial destination on error.
    """
    if image.mode not in {"L", "RGB", "RGBA"}:
        raise ValueError("AVIF 保存には L・RGB・RGBA の画像が必要です。")
    if type(speed) is not int or not 0 <= speed <= 10:
        raise ValueError("AVIF の速度は 0 ～ 10 の整数で指定してください。")
    encoder = _encoder_path()
    destination = Path(destination).resolve()
    intermediate: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".image-compressor-avif-",
            suffix=".png",
            dir=destination.parent,
            delete=False,
        ) as handle:
            intermediate = Path(handle.name)
        image.save(
            intermediate,
            format="PNG",
            compress_level=0,
            icc_profile=icc_profile,
            exif=exif,
        )
        # Gray ICC profiles remain attached to grayscale pixels. RGB/RGBA use
        # GBR identity planes; original ICC profiles override CP/TC signaling.
        # Lossless mode checks identity before loading PNG. For monochrome
        # PNGs libavif then normalizes the output matrix to BT.601/400.
        matrix = 6 if image.mode == "L" and quality is not None else 0
        primaries, transfer = (2, 2) if icc_profile else (1, 13)
        codec_options = [
            "--yuv", "400" if image.mode == "L" else "444",
            "--range", "full",
            "--cicp", f"{primaries}/{transfer}/{matrix}",
            "--qalpha", "100",
        ]
        if quality is None:
            codec_options.append("--lossless")
        else:
            codec_options.extend(("--qcolor", str(quality)))
        process = subprocess.run(
            [
                str(encoder),
                "--codec",
                "aom",
                "--jobs",
                "4",
                "--speed",
                str(speed),
                *codec_options,
                "--",
                str(intermediate),
                str(destination),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=900,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()[:1600]
            raise ValueError(f"AVIF 保存に失敗しました。（{detail}）")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ValueError("AVIF 保存結果を取得できませんでした。")
    except subprocess.TimeoutExpired as exc:
        raise ValueError("AVIF 保存が 15 分以内に完了しませんでした。") from exc
    except OSError as exc:
        raise ValueError(f"AVIF 保存を実行できませんでした。（{exc}）") from exc
    finally:
        if intermediate is not None:
            intermediate.unlink(missing_ok=True)
