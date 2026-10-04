"""Local image conversion with safe output naming and explicit metadata handling."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
import time
from typing import Iterable, Literal

from PIL import ExifTags, Image, UnidentifiedImageError

from .avif_lossless import avif_properties as _avif_properties, encode_lossless_avif, encode_lossy_avif


OutputFormat = Literal["AVIF", "HEIF", "JPEG", "WEBP", "PNG"]
INPUT_EXTENSIONS = frozenset(
    {".avif", ".heif", ".heic", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
)
_EXTENSIONS = {"AVIF": ".avif", "HEIF": ".heif", "JPEG": ".jpg", "WEBP": ".webp", "PNG": ".png"}
# Windows can reject simultaneous replacements of the same destination. Only
# the final rename is serialized; image decoding, encoding and verification
# remain independent, and a failed publication never removes existing files.
_PUBLICATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class CompressionSettings:
    output_format: OutputFormat = "AVIF"
    quality: int = 65
    speed: int = 6
    scale_percent: int = 100
    preserve_metadata: bool = False
    preserve_image: bool = True
    overwrite_original: bool = False

    def __post_init__(self) -> None:
        if self.output_format not in _EXTENSIONS:
            raise ValueError("出力形式は AVIF・HEIF・JPEG・WEBP・PNG から選んでください。")
        if type(self.quality) is not int or not 0 <= self.quality <= 100:
            raise ValueError("品質は 0 ～ 100 の整数で指定してください。")
        if type(self.speed) is not int or not 0 <= self.speed <= 10:
            raise ValueError("圧縮速度は 0 ～ 10 の整数で指定してください。")
        if type(self.scale_percent) is not int or not 1 <= self.scale_percent <= 100:
            raise ValueError("画像サイズの割合は 1 ～ 100 の整数で指定してください。")
        if type(self.preserve_metadata) is not bool:
            raise ValueError("メタデータ保持は True または False で指定してください。")
        if type(self.preserve_image) is not bool:
            raise ValueError("元の画像の保持は True または False で指定してください。")
        if type(self.overwrite_original) is not bool:
            raise ValueError("元ファイルの上書きは True または False で指定してください。")


@dataclass(frozen=True)
class CompressionResult:
    input_path: Path
    output_path: Path
    input_bytes: int
    output_bytes: int
    width: int
    height: int
    backend: str = "CPU"

    @property
    def uses_gpu(self) -> bool:
        return self.backend.startswith("GPU")

    @property
    def savings_percent(self) -> float:
        """Negative percentages mean the encoded image is larger than its source."""
        if self.input_bytes <= 0:
            return 0.0
        return (1 - self.output_bytes / self.input_bytes) * 100


@lru_cache(maxsize=1)
def available_formats() -> tuple[str, ...]:
    """Probe installed encoders, including builds with decode-only codecs."""
    formats: list[str] = []
    with Image.new("RGB", (2, 2), "white") as sample:
        for output_format in _EXTENSIONS:
            try:
                with BytesIO() as buffer:
                    if output_format == "AVIF":
                        from PIL import AvifImagePlugin

                        options = {"speed": 10, "max_threads": 1}
                    elif output_format == "HEIF":
                        from .heif import heif_save_options

                        options = heif_save_options(quality=65, speed=10, preserve_image=True,
                                                    has_alpha=False, icc_profile=None, exif=b"")
                    else:
                        if output_format == "JPEG":
                            from PIL import JpegImagePlugin
                        elif output_format == "WEBP":
                            from PIL import WebPImagePlugin
                        else:
                            from PIL import PngImagePlugin
                        options = {}
                    # Import only the candidate codec. Image.save otherwise
                    # initializes every Pillow plugin when a format is absent.
                    # Registration alone is not proof that encoding works.
                    if output_format not in Image.SAVE:
                        continue
                    sample.save(buffer, format=output_format, **options)
                formats.append(output_format)
            except (ImportError, OSError, ValueError, KeyError, RuntimeError):
                continue
    return tuple(formats)


def try_hardware_encode(*args, **kwargs):
    """Load GPU process helpers when hardware compression is first requested."""
    from .hardware import try_hardware_encode as encode

    return encode(*args, **kwargs)


def collect_images(paths: Iterable[Path]) -> list[Path]:
    """Expand folders recursively and deduplicate resolved, supported files."""
    result: list[Path] = []
    seen: set[str] = set()
    for supplied_path in paths:
        path = Path(supplied_path).expanduser()
        try:
            if path.is_dir():
                candidates = sorted(
                    path.rglob("*"), key=lambda item: (str(item).casefold(), str(item))
                )
            else:
                candidates = [path]
            for candidate in candidates:
                if candidate.suffix.lower() not in INPUT_EXTENSIONS or not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                identity = os.path.normcase(str(resolved))
                if identity not in seen:
                    seen.add(identity)
                    result.append(resolved)
        except OSError:
            # One inaccessible folder must not prevent processing other selections.
            continue
    return result


def _tiff_photo_exif(original: Image.Image) -> Image.Exif:
    """Copy photographic EXIF while excluding TIFF pixel storage directories."""
    source_exif = original.getexif()
    metadata = Image.Exif()
    photo_tags = (
        ExifTags.Base.ImageWidth, ExifTags.Base.ImageLength,
        ExifTags.Base.DocumentName, ExifTags.Base.ImageDescription,
        ExifTags.Base.Make, ExifTags.Base.Model,
        ExifTags.Base.XResolution, ExifTags.Base.YResolution, ExifTags.Base.ResolutionUnit,
        ExifTags.Base.Software, ExifTags.Base.DateTime, ExifTags.Base.Artist,
        ExifTags.Base.Copyright, ExifTags.Base.XPTitle, ExifTags.Base.XPComment,
        ExifTags.Base.XPAuthor, ExifTags.Base.XPKeywords, ExifTags.Base.XPSubject,
        ExifTags.Base.Rating, ExifTags.Base.RatingPercent, ExifTags.Base.ColorSpace,
    )
    for tag in photo_tags:
        if tag in source_exif:
            metadata[tag] = source_exif[tag]
    for tag in (ExifTags.IFD.Exif, ExifTags.IFD.GPSInfo):
        if tag in source_exif:
            fields = source_exif.get_ifd(tag).copy()
            if tag == ExifTags.IFD.Exif and ExifTags.IFD.Interop in fields:
                fields[ExifTags.IFD.Interop] = source_exif.get_ifd(ExifTags.IFD.Interop).copy()
            metadata[tag] = fields
    return metadata


def _load_image(source: Path) -> Image.Image:
    from PIL import ImageOps

    # Also support callers of this internal loader before the format catalog
    # has been probed. Explicit HEIF imports retain opener registration.
    if source.suffix.lower() in {".heif", ".heic"}:
        from .heif import register_heif_support

        register_heif_support()
    try:
        # Explicit streams also avoid TIFF's mmap path, which can interpret
        # oriented RGBA tiles with swapped dimensions and keep source locked.
        with source.open("rb") as stream, Image.open(stream) as original:
            if getattr(original, "n_frames", 1) > 1:
                raise ValueError(
                    "複数フレームの画像（アニメーション・複数ページ）は対応していません。"
                )
            if original.format == "HEIF":
                from .heif import validate_heif_input

                validate_heif_input(original)
            # TIFF keeps metadata in tag_v2 rather than info['exif']; copying
            # pixels alone loses it. Materialize nested IFDs before load can
            # close the source stream, without carrying strip/tile offsets.
            tiff_exif = _tiff_photo_exif(original) if original.format == "TIFF" else None
            if tiff_exif is not None:
                # Pillow's TIFF decoder already normalizes orientation. Its
                # metadata cache must not request a second rotation below.
                original.load()
                original.getexif().pop(ExifTags.Base.Orientation, None)
            # Pixel orientation is applied before resizing, then removed from EXIF.
            oriented = ImageOps.exif_transpose(original)
            if tiff_exif:
                serialized = tiff_exif.tobytes()
                oriented.info["exif"] = serialized
                oriented.getexif().load(serialized)
            return oriented
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError(f"画像を読み込めませんでした: {source.name}（{exc}）") from exc


def _png_color_chunks(source: Path) -> tuple[int | None, tuple[tuple[bytes, bytes], ...]]:
    """Read color interpretation chunks verbatim, without copying private text."""
    chunks: list[tuple[bytes, bytes]] = []
    bit_depth = None
    with source.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            return None, ()
        while header := stream.read(8):
            if len(header) != 8:
                break
            length, kind = struct.unpack(">I4s", header)
            if kind in {b"IDAT", b"IEND"}:
                break
            if kind == b"IHDR" or kind in {b"gAMA", b"cHRM", b"sRGB", b"cICP", b"sBIT"}:
                # These standardized chunks have fixed small payloads.
                if length > 32:
                    raise ValueError("PNG の色情報を読み込めませんでした。")
                payload = stream.read(length)
                if len(payload) != length:
                    raise ValueError("PNG の色情報を読み込めませんでした。")
                if kind == b"IHDR" and len(payload) == 13:
                    bit_depth = payload[8]
                elif kind != b"IHDR":
                    chunks.append((kind, payload))
            else:
                stream.seek(length, os.SEEK_CUR)
            stream.seek(4, os.SEEK_CUR)
    return bit_depth, tuple(chunks)



def _preservation_error(reason: str, alternative: str | None = "PNG または WebP") -> ValueError:
    advice = f" 元の画像を保持するには {alternative} を選んでください。" if alternative else ""
    return ValueError(reason + advice)


def _prepare_preserved_image(
    image: Image.Image, source: Path, output_format: str,
    png_chunks: tuple[tuple[bytes, bytes], ...], bit_depth: int | None,
) -> tuple[Image.Image, bytes | None]:
    if output_format == "JPEG":
        raise _preservation_error("JPEG は画素を完全に保持できません。")
    high_depth_modes = {"I", "I;16", "I;16L", "I;16B", "I;16N"}
    native_png_modes = {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}
    if image.mode not in native_png_modes:
        raise _preservation_error(f"このアプリでは {image.mode} の画素と色情報をそのまま保存できません。", None)
    if bit_depth and bit_depth > 8 and image.mode not in high_depth_modes:
        raise _preservation_error("このアプリでは 16 ビットのカラー画像を完全に読み込めないため保存できません。", None)
    if image.mode in high_depth_modes and output_format != "PNG":
        raise _preservation_error("この形式では 16 ビットの画素を保持できません。", "PNG")
    if image.mode == "I" and any(value < 0 or value > 65535 for value in image.getextrema()):
        raise _preservation_error("このアプリでは 32 ビットの画素を PNG に完全に保持できません。", None)
    # RGB TIFF files can already have been reduced to 8 bits by the decoder.
    if source.suffix.lower() in {".tif", ".tiff"}:
        with Image.open(source) as original:
            depths = original.tag_v2.get(258, (8,))
            if isinstance(depths, int):
                depths = (depths,)
            if any(depth > 16 for depth in depths):
                raise _preservation_error("このアプリでは 32 ビットの TIFF の画素をそのまま保存できません。", None)
            if any(depth > 8 for depth in depths) and image.mode not in high_depth_modes:
                raise _preservation_error("このアプリでは高ビット深度のカラー TIFF を完全に読み込めないため保存できません。", None)
    if source.suffix.lower() == ".avif":
        for property_data in _avif_properties(source):
            if property_data[:4] == b"pixi" and any(depth > 8 for depth in property_data[9:]):
                raise _preservation_error("このアプリでは高ビット深度の AVIF を完全に読み込めないため保存できません。", None)
            if property_data[:4] == b"av1C" and len(property_data) >= 7 and property_data[6] & 0x40:
                raise _preservation_error("このアプリでは高ビット深度の AVIF を完全に読み込めないため保存できません。", None)
            if property_data[:8] == b"colrnclx" and len(property_data) >= 15 and not image.info.get("icc_profile"):
                primaries, transfer, _matrix = struct.unpack(">HHH", property_data[8:14])
                if (primaries, transfer) not in {(1, 13), (2, 2)}:
                    raise _preservation_error("このアプリではこの AVIF の色情報をそのまま保存できません。", None)

    profile = image.info.get("icc_profile") or None
    if not profile:
        exif = image.getexif()
        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
        color_space = exif_ifd.get(ExifTags.Base.ColorSpace, exif.get(ExifTags.Base.ColorSpace))
        interoperability = (
            exif.get_ifd(ExifTags.IFD.Interop).get(1)
            if ExifTags.IFD.Interop in exif_ifd else None
        )
        if isinstance(interoperability, bytes):
            interoperability = interoperability.rstrip(b"\0").decode("ascii", errors="replace")
        if color_space == 2 or interoperability == "R03":
            raise _preservation_error("ICC のない Adobe RGB 画像の色情報をそのまま保存できません。", None)
        if color_space == 65535 and interoperability != "R98":
            raise _preservation_error("ICC のない未校正の EXIF 色情報をそのまま保存できません。", None)
    if profile:
        from PIL import ImageCms

        try:
            color_space = ImageCms.ImageCmsProfile(BytesIO(profile)).profile.xcolor_space.strip()
        except (ImageCms.PyCMSError, OSError, ValueError) as exc:
            raise _preservation_error("埋め込みカラープロファイルを読み込めません。", None) from exc
        expected = "GRAY" if image.mode in {"1", "L", "LA", *high_depth_modes} else "RGB"
        if color_space != expected:
            raise _preservation_error("画素と埋め込みカラープロファイルの種類が一致しません。", None)
        if expected == "GRAY" and output_format != "PNG" and not (
            output_format in {"AVIF", "HEIF"} and image.mode in {"1", "L"} and not image.has_transparency_data
        ):
            raise _preservation_error("グレースケールのカラープロファイルをそのまま保存できません。", "PNG")
    if output_format != "PNG":
        chunks = dict(png_chunks)
        if b"cICP" in chunks and image.info.get("icc_profile"):
            # PNG prioritizes CICP over ICC; AVIF prioritizes ICC over CICP.
            # Keeping only the ICC could therefore change the displayed colors.
            raise _preservation_error("CICP と ICC が併記された PNG の色空間を確実に移せません。", "PNG")
        if b"cICP" in chunks and chunks[b"cICP"] != b"\x01\x0d\x00\x01":
            raise _preservation_error("PNG の CICP 色情報をそのまま移せません。", "PNG")
        if not profile and any(kind in chunks for kind in (b"gAMA", b"cHRM", b"cICP", b"sRGB")):
            if b"sRGB" not in chunks and chunks.get(b"cICP") != b"\x01\x0d\x00\x01":
                raise _preservation_error("PNG のガンマ・色度情報をそのまま移せません。", "PNG")
            from PIL import ImageCms

            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    if output_format == "PNG":
        prepared = image.copy()
    elif (
        output_format in {"AVIF", "HEIF"} and image.mode in {"1", "L"}
        and not image.has_transparency_data
        and (not profile or image.info.get("icc_profile"))
    ):
        prepared = image.convert("L")
    else:
        prepared = image.convert("RGBA" if image.has_transparency_data else "RGB")
    return prepared, profile


def _verify_preserved_image(
    path: Path, expected: Image.Image, profile: bytes | None,
    settings: CompressionSettings, png_chunks: tuple[tuple[bytes, bytes], ...],
) -> None:
    """Publish only an image whose decoded pixels and color interpretation match."""
    with Image.open(path) as encoded:
        encoded.load()
        if encoded.format != settings.output_format:
            raise ValueError("保存結果の画像形式が指定と一致しません。")
        same_pixels = encoded.size == expected.size
        if same_pixels and expected.mode in {"I", "I;16", "I;16L", "I;16B", "I;16N"}:
            with encoded.convert("I") as actual, expected.convert("I") as original:
                same_pixels = actual.tobytes() == original.tobytes()
        elif same_pixels:
            with encoded.convert("RGBA") as actual, expected.convert("RGBA") as original:
                same_pixels = actual.tobytes() == original.tobytes()
        if not same_pixels or encoded.info.get("icc_profile") != profile:
            raise _preservation_error(
                f"{settings.output_format} の保存結果が元の画素・色情報と一致しません。"
            )
    if settings.output_format == "PNG":
        _, saved_chunks = _png_color_chunks(path)
        # PNG forbids simultaneous ICC and sRGB; the ICC remains authoritative.
        expected_chunks = tuple(chunk for chunk in png_chunks if not (profile and chunk[0] == b"sRGB"))
        if dict(saved_chunks) != dict(expected_chunks):
            raise _preservation_error("PNG の色情報を完全に保存できませんでした。")


def _prepare_mode(image: Image.Image, output_format: str) -> tuple[Image.Image, bytes | None]:
    profile = image.info.get("icc_profile")
    has_alpha = "A" in image.getbands() or "transparency" in image.info

    # A CMYK/LAB profile describes non-RGB values. Convert both pixels and profile
    # together rather than attaching that profile to a simple RGB conversion.
    needs_profile_conversion = image.mode in {"CMYK", "LAB"} or (
        image.mode in {"L", "LA"} and (has_alpha or output_format == "WEBP")
    )
    if profile and needs_profile_conversion:
        from PIL import ImageCms

        try:
            input_profile = ImageCms.ImageCmsProfile(BytesIO(profile))
            output_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            with image.convert("L") if image.mode == "LA" else image.copy() as color_image:
                prepared = ImageCms.profileToProfile(
                    color_image, input_profile, output_profile, outputMode="RGB"
                )
            profile = output_profile.tobytes()
            if has_alpha:
                with image.convert("RGBA") as rgba:
                    prepared.putalpha(rgba.getchannel("A"))
        except (ImageCms.PyCMSError, OSError, ValueError) as exc:
            raise ValueError("埋め込みカラープロファイルを変換できませんでした。") from exc
    elif has_alpha:
        prepared = image.convert("RGBA")
    elif image.mode == "L" and output_format != "WEBP":
        prepared = image.copy()
    else:
        prepared = image.convert("RGB")

    if output_format == "JPEG" and "A" in prepared.getbands():
        with prepared:
            background = Image.new("RGB", prepared.size, "white")
            background.paste(prepared, mask=prepared.getchannel("A"))
        prepared = background
    return prepared, profile


def _prepare_lossy_avif_image(
    image: Image.Image, source: Path,
    png_chunks: tuple[tuple[bytes, bytes], ...], bit_depth: int | None,
    output_format: str = "AVIF",
) -> tuple[Image.Image, bytes | None]:
    """Use the same color/depth checks for lossy AVIF/HEIF as for preservation.

    RGB/gray profiles stay unchanged. When their original color model cannot
    be stored by AVIF, the established CMS conversion moves pixels and profile
    to sRGB together; quantization never authorizes discarding the color space.
    """
    if image.mode in {"CMYK", "LAB", "LA"} and image.info.get("icc_profile"):
        if any(kind == b"cICP" for kind, _ in png_chunks):
            raise _preservation_error("CICP と ICC が併記された PNG の色空間を確実に移せません。", "PNG")
        converted, profile = _prepare_mode(image, output_format)
        with converted:
            converted.info["icc_profile"] = profile
            return _prepare_preserved_image(converted, source, output_format, png_chunks, bit_depth)
    return _prepare_preserved_image(image, source, output_format, png_chunks, bit_depth)


def _verify_lossy_avif(
    path: Path, expected: Image.Image, profile: bytes | None,
    *, hardware: bool = False,
) -> None:
    """Check color signaling and alpha while allowing quantization of RGB."""
    with Image.open(path) as encoded:
        encoded.load()
        if encoded.size != expected.size or encoded.info.get("icc_profile") != profile:
            raise ValueError("AVIF の画像サイズ・カラープロファイルを正しく保存できませんでした。")
        expected_color_space = "L" if expected.mode == "L" else "RGB"
        actual_color_space = "L" if encoded.mode == "L" else "RGB"
        if actual_color_space != expected_color_space:
            raise ValueError("AVIF の画素とカラープロファイルの種類が一致しません。")
        if expected.mode == "RGBA":
            with encoded.convert("RGBA") as actual:
                if actual.getchannel("A").tobytes() != expected.getchannel("A").tobytes():
                    raise ValueError("AVIF の透明度を正しく保存できませんでした。")
    primaries, transfer = (2, 2) if profile else (1, 13)
    matrix = 6 if expected.mode == "L" else (1 if hardware else 0)
    nclx = b"colrnclx" + struct.pack(">HHHB", primaries, transfer, matrix, 0x80)
    if nclx not in _avif_properties(path):
        raise ValueError("AVIF の色空間・色変換・明るさの範囲を正しく保存できませんでした。")


def _save_options(
    settings: CompressionSettings, profile: bytes | None, exif: Image.Exif, size: tuple[int, int]
) -> dict[str, object]:
    options: dict[str, object] = {"icc_profile": profile, "exif": b""}
    if settings.preserve_metadata and exif:
        exif.pop(ExifTags.Base.Orientation, None)
        for tag, dimension in ((ExifTags.Base.ImageWidth, size[0]), (ExifTags.Base.ImageLength, size[1])):
            if tag in exif:
                exif[tag] = dimension
        if ExifTags.IFD.Exif in exif:
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            for tag, dimension in ((ExifTags.Base.ExifImageWidth, size[0]), (ExifTags.Base.ExifImageHeight, size[1])):
                if tag in exif_ifd:
                    exif_ifd[tag] = dimension
        options["exif"] = exif.tobytes()
    if settings.output_format == "AVIF":
        options.update(quality=settings.quality, speed=settings.speed, max_threads=4)
    elif settings.output_format == "JPEG":
        options.update(quality=settings.quality, optimize=True, progressive=True)
    elif settings.output_format == "WEBP":
        options.update(quality=settings.quality, method=6)
        if settings.preserve_image:
            options.update(lossless=True, exact=True, quality=100)
    elif settings.output_format == "PNG":
        options.update(optimize=True, compress_level=9)
    return options


def _verify_encoded_image(
    path: Path, expected: Image.Image, output_format: str,
    profile: bytes | None, exif: bytes,
) -> None:
    """Decode CPU/GPU output before allowing it to replace an original file."""
    with Image.open(path) as encoded:
        encoded.load()
        if encoded.format != output_format or encoded.size != expected.size:
            raise ValueError("保存結果の画像形式・サイズが指定と一致しません。")
        if encoded.info.get("icc_profile") != profile or (encoded.info.get("exif") or b"") != exif:
            raise ValueError("保存結果のカラープロファイル・撮影情報が指定と一致しません。")
        if "A" in expected.getbands():
            with encoded.convert("RGBA") as decoded:
                if decoded.getchannel("A").tobytes() != expected.getchannel("A").tobytes():
                    raise ValueError("保存結果の透明度が指定と一致しません。")


def _same_file_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def _replace_file(first: Path, second: Path) -> None:
    """Bound Windows sharing-lock retries; permanent failures still propagate."""
    for attempt in range(5):
        try:
            os.replace(first, second)
            return
        except OSError as error:
            if sys.platform != "win32" or getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 4:
                raise
            time.sleep(0.025 * (attempt + 1))


def _publish_image(
    temporary_path: Path, output_path: Path, source: Path, overwrite_original: bool,
) -> None:
    """Publish a verified image and restore any old output if source removal fails."""
    with _PUBLICATION_LOCK:
        if not overwrite_original or _same_file_path(source, output_path):
            _replace_file(temporary_path, output_path)
            return
        backup_path = None
        published = False
        source_removed = False
        try:
            if output_path.exists():
                with tempfile.NamedTemporaryFile(
                    prefix=".image-compressor-", suffix=".bak", dir=output_path.parent, delete=False,
                ) as backup:
                    backup_path = Path(backup.name)
                shutil.copy2(output_path, backup_path)
            _replace_file(temporary_path, output_path)
            published = True
            try:
                source.unlink()
                source_removed = True
            except OSError:
                try:
                    if backup_path is not None:
                        _replace_file(backup_path, output_path)
                    else:
                        output_path.unlink()
                except OSError as rollback_error:
                    recovery = f"以前の出力の退避ファイル: {backup_path}" if backup_path else f"作成した出力: {output_path}"
                    raise ValueError(
                        f"元画像を削除できず、出力も元に戻せませんでした。元画像は残っています。\n{recovery}"
                    ) from rollback_error
                raise
        finally:
            if backup_path is not None and (not published or source_removed):
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    # A completed conversion must not become an error because
                    # Windows refuses removal of its disposable backup.
                    pass


def convert_image(
    source: Path, settings: CompressionSettings, output_dir: Path | None = None
) -> CompressionResult:
    """Encode and validate in a separate file, then atomically replace the output.

    Output uses the source stem and selected extension. The source is protected
    unless its replacement is explicitly enabled; replacing it uses its folder
    and removes a differing original extension only after valid publication.
    """
    if not isinstance(settings, CompressionSettings):
        raise TypeError("settings には CompressionSettings を指定してください。")
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"画像ファイルが見つかりません: {source}")
    if source.suffix.lower() not in INPUT_EXTENSIONS:
        raise ValueError(f"対応していないファイル形式です: {source.suffix or '拡張子なし'}")
    if settings.output_format not in available_formats():
        raise ValueError(f"この環境では {settings.output_format} の保存機能が利用できません。")
    input_bytes = source.stat().st_size
    destination_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None and not settings.overwrite_original else source.parent
    )
    output_path = destination_dir / f"{source.stem}{_EXTENSIONS[settings.output_format]}"
    if not settings.overwrite_original and _same_file_path(source, output_path):
        raise ValueError("入力と保存先が同じです。「元ファイルを上書き」を有効にするか、別の保存先を選択してください。")
    temporary_path: Path | None = None
    try:
        with _load_image(source) as oriented:
            exif = oriented.getexif()
            bit_depth, png_chunks = _png_color_chunks(source)
            if settings.preserve_image:
                prepared, profile = _prepare_preserved_image(
                    oriented, source, settings.output_format, png_chunks, bit_depth
                )
            elif settings.output_format in {"AVIF", "HEIF"}:
                prepared, profile = _prepare_lossy_avif_image(
                    oriented, source, png_chunks, bit_depth, settings.output_format
                )
                if profile != (oriented.info.get("icc_profile") or None) and oriented.mode in {"CMYK", "LAB", "LA"}:
                    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
                    if ExifTags.Base.ColorSpace in exif_ifd:
                        exif_ifd[ExifTags.Base.ColorSpace] = 1
                    if ExifTags.Base.ColorSpace in exif:
                        exif[ExifTags.Base.ColorSpace] = 1
                    if ExifTags.IFD.Interop in exif_ifd:
                        interop_ifd = exif.get_ifd(ExifTags.IFD.Interop)
                        if 1 in interop_ifd:
                            interop_ifd[1] = "R98"
            else:
                prepared, profile = _prepare_mode(oriented, settings.output_format)
            if not settings.preserve_image and settings.scale_percent != 100:
                # Round each oriented dimension half up using integer math;
                # thin images keep at least one pixel along either axis.
                size = tuple(max(1, (edge * settings.scale_percent + 50) // 100) for edge in oriented.size)
                with prepared:
                    prepared = prepared.resize(size, Image.Resampling.LANCZOS)
            with prepared:
                options = _save_options(settings, profile, exif, prepared.size)
                if settings.preserve_image and settings.output_format == "PNG":
                    from PIL.PngImagePlugin import PngInfo

                    color_info = PngInfo()
                    for kind, payload in png_chunks:
                        color_info.add(kind, payload)
                    options["pnginfo"] = color_info
                    if "transparency" in oriented.info:
                        options["transparency"] = oriented.info["transparency"]
                # AVIF/PNG can fall back to image.info for metadata; clear it too.
                prepared.info.clear()
                destination_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    prefix=".image-compressor-", suffix=".tmp", dir=destination_dir, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                backend = "CPU"
                if not settings.preserve_image and settings.output_format in {"AVIF", "HEIF", "JPEG"}:
                    backend = try_hardware_encode(
                        prepared, temporary_path, output_format=settings.output_format,
                        quality=settings.quality, speed=settings.speed,
                        icc_profile=profile, exif=options["exif"],
                        verify_output=(
                            lambda path: _verify_lossy_avif(path, prepared, profile, hardware=True)
                        ) if settings.output_format == "AVIF" else None,
                    ) or "CPU"
                if backend == "CPU":
                    if settings.output_format == "AVIF":
                        if settings.preserve_image:
                            encode_lossless_avif(
                                prepared, temporary_path, speed=settings.speed,
                                icc_profile=profile, exif=options["exif"],
                            )
                        else:
                            encode_lossy_avif(
                                prepared, temporary_path, quality=settings.quality, speed=settings.speed,
                                icc_profile=profile, exif=options["exif"],
                            )
                            _verify_lossy_avif(temporary_path, prepared, profile)
                    elif settings.output_format == "HEIF":
                        from .heif import heif_save_options, verify_heif_output

                        heif_options = heif_save_options(
                            quality=settings.quality, speed=settings.speed,
                            preserve_image=settings.preserve_image, has_alpha=prepared.mode == "RGBA",
                            icc_profile=profile, exif=options["exif"],
                        )
                        prepared.save(temporary_path, format="HEIF", **heif_options)
                        verify_heif_output(temporary_path, prepared, icc_profile=profile, exif=options["exif"])
                    else:
                        prepared.save(temporary_path, format=settings.output_format, **options)
                if settings.preserve_image:
                    _verify_preserved_image(temporary_path, oriented, profile, settings, png_chunks)
                elif settings.output_format in {"JPEG", "WEBP", "PNG"}:
                    _verify_encoded_image(temporary_path, prepared, settings.output_format, profile, options["exif"])
                output_bytes = temporary_path.stat().st_size
                _publish_image(temporary_path, output_path, source, settings.overwrite_original)
                return CompressionResult(
                    source, output_path, input_bytes, output_bytes, prepared.width, prepared.height, backend
                )
    except OSError as exc:
        raise ValueError(f"画像を保存できませんでした: {source.name}（{exc}）") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
