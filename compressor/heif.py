"""HEIF/HEIC support with explicit color, metadata and alpha verification."""

from __future__ import annotations

from pathlib import Path
import struct

from PIL import Image

from .avif_lossless import avif_properties


def register_heif_support() -> bool:
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return False
    register_heif_opener(thumbnails=False, aux_images=False, depth_images=False, decode_threads=4)
    return True


def validate_heif_input(image: Image.Image) -> None:
    """Reject source information that Pillow would silently reduce or retag."""
    if image.format != "HEIF":
        return
    if image.info.get("bit_depth", 8) > 8:
        raise ValueError("高ビット深度の HEIF は画素を正確に読み込めないため保存できません。")
    nclx = image.info.get("nclx_profile")
    if nclx and not image.info.get("icc_profile"):
        primaries, transfer = nclx.get("color_primaries"), nclx.get("transfer_characteristics")
        if (primaries, transfer) not in {(1, 13), (2, 2)}:
            raise ValueError("この HEIF の色情報をそのまま保存できません。")


def heif_save_options(
    *, quality: int, speed: int, preserve_image: bool, has_alpha: bool,
    icc_profile: bytes | None, exif: bytes,
) -> dict[str, object]:
    # Pillow-heif applies lossy quality to the auxiliary alpha image too. Its
    # public API cannot encode alpha separately, so preserve the entire image
    # losslessly when alpha is present, then verify its decoded alpha below.
    lossless = preserve_image or has_alpha
    preset = "fast" if speed >= 8 else "medium" if speed >= 6 else "slow" if speed >= 4 else "slower"
    return {
        "quality": -1 if lossless else quality, "chroma": 444,
        "matrix_coefficients": 0, "color_primaries": 2 if icc_profile else 1,
        "transfer_characteristics": 2 if icc_profile else 13, "full_range_flag": 1,
        "icc_profile": icc_profile, "exif": exif or None, "xmp": None,
        "save_nclx_profile": True, "tile_size": 0,
        "enc_params": {"preset": preset, "x265:pools": "4", "x265:frame-threads": "1", "x265:log-level": "error"},
    }


def verify_heif_output(
    path: Path, expected: Image.Image, *, icc_profile: bytes | None, exif: bytes,
) -> None:
    with Image.open(path) as encoded:
        encoded.load()
        if encoded.format != "HEIF" or encoded.size != expected.size or encoded.info.get("bit_depth") != 8:
            raise ValueError("HEIF の保存結果の形式・画像サイズ・ビット深度が一致しません。")
        if encoded.info.get("icc_profile") != icc_profile or (encoded.info.get("exif") or b"") != exif:
            raise ValueError("HEIF のカラープロファイル・撮影情報を正しく保存できませんでした。")
        if encoded.info.get("xmp"):
            raise ValueError("HEIF の不要なメタデータを除去できませんでした。")
        if expected.mode == "L" and encoded.mode != "L":
            raise ValueError("HEIF のグレースケールを正しく保存できませんでした。")
        if expected.mode == "RGBA":
            with encoded.convert("RGBA") as actual:
                if actual.getchannel("A").tobytes() != expected.getchannel("A").tobytes():
                    raise ValueError("HEIF の透明度を正しく保存できませんでした。")
    if not icc_profile:
        nclx = b"colrnclx" + struct.pack(">HHHB", 1, 13, 0, 0x80)
        if nclx not in avif_properties(path):
            raise ValueError("HEIF の色空間・色変換・明るさの範囲を正しく保存できませんでした。")


register_heif_support()
