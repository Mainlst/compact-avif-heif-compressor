"""Remux one HEVC intra frame from FFmpeg MP4 into a HEIF image item.

No pixels are reencoded here. Only a single local hvc1 track/sample with an
8-bit 4:2:0 configuration and explicit sRGB/BT.709 full-range color is accepted.
The HEIF item/property structure follows ISO BMFF as implemented by libheif.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct


_NCLX = b"nclx" + struct.pack(">HHHB", 1, 13, 1, 0x80)
_MAX_CONTAINER_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class _Box:
    kind: bytes
    payload: int
    end: int


def _boxes(data: bytes, start: int = 0, end: int | None = None) -> tuple[_Box, ...]:
    end = len(data) if end is None else end
    if not 0 <= start <= end <= len(data):
        raise ValueError("HEIF のボックス範囲が不正です。")
    result = []
    while start < end:
        if start + 8 > end:
            raise ValueError("HEIF のボックスヘッダーが不足しています。")
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise ValueError("HEIF のボックスサイズが不足しています。")
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError("HEIF のボックスサイズが不正です。")
        result.append(_Box(kind, start + header, start + size))
        start += size
    return tuple(result)


def _one(boxes: tuple[_Box, ...], kind: bytes) -> _Box:
    matching = [box for box in boxes if box.kind == kind]
    if len(matching) != 1:
        raise ValueError(f"HEIF 生成には {kind.decode('ascii', errors='replace')} が 1 個必要です。")
    return matching[0]


def _box(kind: bytes, payload: bytes) -> bytes:
    if len(payload) + 8 > 0xFFFFFFFF:
        raise ValueError("HEIF のデータが大きすぎます。")
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _fullbox(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return _box(kind, bytes((version, 0, 0, 0)) + payload)


def _read(path: Path) -> bytes:
    if path.stat().st_size > _MAX_CONTAINER_BYTES:
        raise ValueError("ハードウェア保存結果が大きすぎます。")
    return path.read_bytes()


def _validate_configuration(configuration: bytes) -> None:
    # HEVCDecoderConfigurationRecord: chromaFormat=1, both bitDepthMinus8=0.
    if len(configuration) < 23 or configuration[0] != 1 or configuration[1] & 31 not in {1, 3} or (
        configuration[16] & 3 != 1 or configuration[17] & 7 or configuration[18] & 7
    ):
        raise ValueError("GPU の HEVC 設定が 8 ビット・4:2:0 と一致しません。")
    position = 23
    parameter_sets: set[int] = set()
    for _ in range(configuration[22]):
        if position + 3 > len(configuration):
            raise ValueError("HEVC のパラメーターセットが不足しています。")
        nal_type = configuration[position] & 0x3F
        count = struct.unpack_from(">H", configuration, position + 1)[0]
        position += 3
        for _ in range(count):
            if position + 2 > len(configuration):
                raise ValueError("HEVC のパラメーター長が不足しています。")
            length = struct.unpack_from(">H", configuration, position)[0]
            position += 2
            if length < 2 or position + length > len(configuration):
                raise ValueError("HEVC のパラメーター長が不正です。")
            if configuration[position] >> 1 & 0x3F != nal_type:
                raise ValueError("HEVC のパラメーター種類が一致しません。")
            parameter_sets.add(nal_type)
            position += length
    if position != len(configuration) or not {32, 33, 34} <= parameter_sets:
        raise ValueError("HEVC の VPS・SPS・PPS を取得できませんでした。")


def _validate_sample(sample: bytes, configuration: bytes) -> None:
    length_size = (configuration[21] & 3) + 1
    position = 0
    intra_frame = False
    while position < len(sample):
        if position + length_size > len(sample):
            raise ValueError("HEVC の画像データ長が不足しています。")
        length = int.from_bytes(sample[position:position + length_size], "big")
        position += length_size
        if length < 2 or position + length > len(sample):
            raise ValueError("HEVC の画像データ長が不正です。")
        nal_type = sample[position] >> 1 & 0x3F
        if nal_type <= 31:
            if not 16 <= nal_type <= 23:
                raise ValueError("HEIF には独立して読める HEVC フレームが必要です。")
            intra_frame = True
        position += length
    if not intra_frame:
        raise ValueError("HEVC の画像フレームがありません。")


def _extract_mp4(data: bytes, size: tuple[int, int]) -> tuple[bytes, bytes]:
    top = _boxes(data)
    table = _one(top, b"moov")
    for kind in (b"trak", b"mdia", b"minf", b"stbl"):
        table = _one(_boxes(data, table.payload, table.end), kind)
    entries = _boxes(data, table.payload, table.end)
    stsd = _one(entries, b"stsd")
    if stsd.end - stsd.payload < 8 or data[stsd.payload:stsd.payload + 4] != b"\0" * 4 or struct.unpack_from(">I", data, stsd.payload + 4)[0] != 1:
        raise ValueError("HEVC のサンプル記述が不正です。")
    sample_entry = _one(_boxes(data, stsd.payload + 8, stsd.end), b"hvc1")
    if sample_entry.end - sample_entry.payload < 78:
        raise ValueError("HEVC のサンプル記述が不足しています。")
    if struct.unpack_from(">HH", data, sample_entry.payload + 24) != size:
        raise ValueError("HEVC の画像サイズが一致しません。")
    properties = _boxes(data, sample_entry.payload + 78, sample_entry.end)
    config_box = _one(properties, b"hvcC")
    configuration = data[config_box.payload:config_box.end]
    _validate_configuration(configuration)
    color_box = _one(properties, b"colr")
    if data[color_box.payload:color_box.end] != _NCLX:
        raise ValueError("GPU の HEVC 色情報が sRGB・BT.709・フルレンジと一致しません。")

    stsz = _one(entries, b"stsz")
    payload = data[stsz.payload:stsz.end]
    if len(payload) < 12 or payload[:4] != b"\0" * 4:
        raise ValueError("HEVC のサンプルサイズが不正です。")
    sample_size, count = struct.unpack_from(">II", payload, 4)
    if count != 1:
        raise ValueError("HEIF 生成には HEVC フレームが 1 枚必要です。")
    if sample_size == 0:
        if len(payload) != 16:
            raise ValueError("HEVC のサンプルサイズが不足しています。")
        sample_size = struct.unpack_from(">I", payload, 12)[0]
    elif len(payload) != 12:
        raise ValueError("HEVC のサンプルサイズが不正です。")
    offsets = [box for box in entries if box.kind in {b"stco", b"co64"}]
    if len(offsets) != 1:
        raise ValueError("HEVC のデータ位置を取得できませんでした。")
    offset_box = offsets[0]
    payload = data[offset_box.payload:offset_box.end]
    offset_size = 4 if offset_box.kind == b"stco" else 8
    if len(payload) != 8 + offset_size or payload[:8] != b"\0" * 4 + struct.pack(">I", 1):
        raise ValueError("HEVC のデータ位置が不正です。")
    offset = int.from_bytes(payload[8:], "big")
    if sample_size == 0 or not any(
        box.kind == b"mdat" and box.payload <= offset < offset + sample_size <= box.end
        for box in top
    ):
        raise ValueError("HEVC の画像データ範囲が不正です。")
    sample = data[offset:offset + sample_size]
    _validate_sample(sample, configuration)
    return configuration, sample


def mux_heif(mp4_path: Path, destination: Path, size: tuple[int, int]) -> None:
    """Write one standards-based HEVC image item; reject ambiguous MP4 inputs."""
    if any(type(dimension) is not int or not 0 < dimension <= 65535 for dimension in size):
        raise ValueError("HEIF の画像サイズが不正です。")
    configuration, sample = _extract_mp4(_read(mp4_path), size)
    ftyp = _box(b"ftyp", b"heic" + b"\0" * 4 + b"mif1heic")
    hdlr = _fullbox(b"hdlr", b"\0" * 4 + b"pict" + b"\0" * 12 + b"ImageCompressor\0")
    pitm = _fullbox(b"pitm", struct.pack(">H", 1))
    infe = _fullbox(b"infe", struct.pack(">HH", 1, 0) + b"hvc1Image\0", version=2)
    iinf = _fullbox(b"iinf", struct.pack(">H", 1) + infe)
    ipco = _box(b"ipco", b"".join((
        _box(b"hvcC", configuration),
        _fullbox(b"ispe", struct.pack(">II", *size)),
        _box(b"colr", _NCLX),
        _fullbox(b"pixi", b"\x03\x08\x08\x08"),
    )))
    ipma = _fullbox(b"ipma", struct.pack(">IH", 1, 1) + b"\x04\x81\x82\x03\x04")
    iprp = _box(b"iprp", ipco + ipma)

    def meta(offset: int) -> bytes:
        iloc = _fullbox(b"iloc", b"\x44\0" + struct.pack(">HHHHII", 1, 1, 0, 1, offset, len(sample)))
        return _fullbox(b"meta", hdlr + pitm + iloc + iinf + iprp)

    item_offset = len(ftyp) + len(meta(0)) + 8
    destination.write_bytes(ftyp + meta(item_offset) + _box(b"mdat", sample))


def heif_properties(path: Path) -> tuple[bytes, ...]:
    """Expose image configuration/color/dimensions for output verification."""
    data = _read(path)
    meta = _one(_boxes(data), b"meta")
    iprp = _one(_boxes(data, meta.payload + 4, meta.end), b"iprp")
    ipco = _one(_boxes(data, iprp.payload, iprp.end), b"ipco")
    return tuple(box.kind + data[box.payload:box.end] for box in _boxes(data, ipco.payload, ipco.end))
