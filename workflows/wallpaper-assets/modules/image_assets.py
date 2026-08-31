"""Image metadata reader owned by the wallpaper-assets workflow."""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any


# Common display aspect ratios. The labels intentionally keep familiar screen
# naming such as 16x10 and 21x9 even when the numbers are not mathematically
# reduced, because they are more useful for wallpaper filenames.
_COMMON_SCREEN_RATIOS: tuple[tuple[str, float], ...] = (
    ("1x1", 1.0),
    ("5x4", 5 / 4),
    ("4x3", 4 / 3),
    ("3x2", 3 / 2),
    ("16x10", 16 / 10),
    ("16x9", 16 / 9),
    ("2x1", 2.0),
    ("21x9", 21 / 9),
    ("32x9", 32 / 9),
)


def _checked_image_size(width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("图像尺寸无效")
    return width, height


def _read_jpeg_dimensions(path: Path) -> tuple[int, int]:
    sof_markers = {
        *range(0xC0, 0xC4), *range(0xC5, 0xC8),
        *range(0xC9, 0xCC), *range(0xCD, 0xD0),
    }
    with path.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise ValueError("JPEG 文件头无效")
        while True:
            byte = stream.read(1)
            while byte and byte != b"\xff":
                byte = stream.read(1)
            if not byte:
                break
            marker_byte = stream.read(1)
            while marker_byte == b"\xff":
                marker_byte = stream.read(1)
            if not marker_byte:
                break
            marker = marker_byte[0]
            if marker == 0xD8 or marker == 0xD9:
                continue
            if marker == 0xDA:
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            length_data = stream.read(2)
            if len(length_data) != 2:
                break
            length = struct.unpack(">H", length_data)[0]
            if length < 2:
                raise ValueError("JPEG 段长度无效")
            segment = stream.read(length - 2)
            if len(segment) != length - 2:
                break
            if marker in sof_markers and len(segment) >= 5:
                height, width = struct.unpack_from(">HH", segment, 1)
                return _checked_image_size(width, height)
    raise ValueError("JPEG 未找到尺寸信息")


def _read_tiff_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        byte_order = stream.read(2)
        if byte_order == b"II":
            endian = "<"
        elif byte_order == b"MM":
            endian = ">"
        else:
            raise ValueError("TIFF 字节序无效")
        if len(stream.read(2)) != 2:
            raise ValueError("TIFF 文件头不完整")
        stream.seek(2)
        if struct.unpack(endian + "H", stream.read(2))[0] != 42:
            raise ValueError("不支持的 TIFF 版本")
        ifd_offset_data = stream.read(4)
        if len(ifd_offset_data) != 4:
            raise ValueError("TIFF IFD 偏移无效")
        stream.seek(struct.unpack(endian + "I", ifd_offset_data)[0])
        count_data = stream.read(2)
        if len(count_data) != 2:
            raise ValueError("TIFF IFD 不完整")
        count = struct.unpack(endian + "H", count_data)[0]
        dimensions: dict[int, int] = {}
        type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
        for _ in range(count):
            entry = stream.read(12)
            if len(entry) != 12:
                break
            tag, value_type = struct.unpack_from(endian + "HH", entry)
            value_count = struct.unpack_from(endian + "I", entry, 4)[0]
            value_size = type_sizes.get(value_type, 0) * value_count
            if not value_size:
                continue
            if value_size <= 4:
                raw = entry[8:8 + value_size]
            else:
                offset = struct.unpack_from(endian + "I", entry, 8)[0]
                current = stream.tell()
                stream.seek(offset)
                raw = stream.read(min(value_size, 8))
                stream.seek(current)
            if value_type == 3 and len(raw) >= 2:
                value = struct.unpack(endian + "H", raw[:2])[0]
            elif value_type == 4 and len(raw) >= 4:
                value = struct.unpack(endian + "I", raw[:4])[0]
            else:
                continue
            if tag in {256, 257}:
                dimensions[tag] = value
        return _checked_image_size(dimensions.get(256, 0), dimensions.get(257, 0))


def _read_webp_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:1024 * 1024]
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("WebP 文件头无效")
    offset = 12
    while offset + 8 <= len(data):
        chunk_type = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        start = offset + 8
        payload = data[start:start + size]
        if chunk_type == b"VP8X" and len(payload) >= 10:
            width = 1 + int.from_bytes(payload[4:7], "little")
            height = 1 + int.from_bytes(payload[7:10], "little")
            return _checked_image_size(width, height)
        if chunk_type == b"VP8 " and len(payload) >= 14:
            signature = payload.find(b"\x9d\x01\x2a", 0, 32)
            if signature >= 0 and len(payload) >= signature + 7:
                width = struct.unpack_from("<H", payload, signature + 3)[0] & 0x3FFF
                height = struct.unpack_from("<H", payload, signature + 5)[0] & 0x3FFF
                return _checked_image_size(width, height)
        if chunk_type == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return _checked_image_size(width, height)
        offset = start + size + (size & 1)
    raise ValueError("WebP 未找到尺寸信息")


def read_image_dimensions(path: str | Path) -> tuple[int, int]:
    """Read image dimensions from headers without decoding pixel data."""
    source = Path(path)
    with source.open("rb") as stream:
        header = stream.read(32)
    if header[:2] == b"\xff\xd8":
        return _read_jpeg_dimensions(source)
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        if len(header) < 24 or header[12:16] != b"IHDR":
            raise ValueError("PNG 缺少 IHDR 尺寸信息")
        return _checked_image_size(struct.unpack_from(">II", header, 16)[0], struct.unpack_from(">II", header, 16)[1])
    if header[:6] in {b"GIF87a", b"GIF89a"}:
        if len(header) < 10:
            raise ValueError("GIF 文件头不完整")
        return _checked_image_size(struct.unpack_from("<HH", header, 6)[0], struct.unpack_from("<HH", header, 6)[1])
    if header[:2] == b"BM":
        if len(header) < 26:
            raise ValueError("BMP 文件头不完整")
        dib_size = struct.unpack_from("<I", header, 14)[0]
        if dib_size >= 40:
            width, height = struct.unpack_from("<ii", header, 18)
            return _checked_image_size(abs(width), abs(height))
        if dib_size == 12:
            return _checked_image_size(*struct.unpack_from("<HH", header, 18))
        raise ValueError("不支持的 BMP DIB 版本")
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return _read_webp_dimensions(source)
    if header[:2] in {b"II", b"MM"}:
        return _read_tiff_dimensions(source)
    raise ValueError("不支持的图像格式")


def _screen_aspect_ratio_token(width: int, height: int) -> str:
    """Return the nearest familiar screen ratio in width-by-height order."""
    ratio = max(width, height) / min(width, height)
    nearest_label, _nearest_ratio = min(
        _COMMON_SCREEN_RATIOS,
        key=lambda candidate: abs(math.log(ratio / candidate[1])),
    )
    if width < height and nearest_label != "1x1":
        left, right = nearest_label.split("x", 1)
        return f"{right}x{left}"
    return nearest_label


def _image_metadata(path: Path) -> dict[str, Any]:
    width, height = read_image_dimensions(path)
    divisor = math.gcd(width, height)
    exact_width = width // divisor
    exact_height = height // divisor
    orientation = "landscape" if width > height else "portrait" if width < height else "square"
    return {
        "available": True,
        "width": width,
        "height": height,
        "orientation": orientation,
        "aspect_ratio": f"{exact_width}:{exact_height}",
        "aspect_ratio_exact": f"{exact_width}x{exact_height}",
        "aspect_ratio_token": _screen_aspect_ratio_token(width, height),
        "aspect_ratio_decimal": f"{width / height:.4f}".rstrip("0").rstrip("."),
    }


def read_metadata(path: str | Path, _root: str | Path | None = None,
                  _options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return image metadata only when the file signature is recognizable."""
    source = Path(path)
    try:
        with source.open("rb") as stream:
            header = stream.read(32)
        recognizable = (
            header[:2] == b"\xff\xd8" or header[:8] == b"\x89PNG\r\n\x1a\n"
            or header[:6] in {b"GIF87a", b"GIF89a"} or header[:2] == b"BM"
            or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
            or header[:2] in {b"II", b"MM"}
        )
        return {"image": _image_metadata(source)} if recognizable else {}
    except (OSError, ValueError, struct.error) as exc:
        return {"image": {"available": False, "error": str(exc)}}
