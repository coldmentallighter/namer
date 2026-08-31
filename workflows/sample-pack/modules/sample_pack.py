"""Metadata reader and helpers owned by the sample-pack workflow."""

from __future__ import annotations

import re
import struct
import wave
from pathlib import Path
from typing import Any


_FILENAME_FIELD_PATTERNS = {
    "bpm": r"(?P<bpm>\d{2,3})(?:\s*BPM)?",
    "key": r"(?P<key>[A-Ga-g](?:#|b)?(?:m|min|minor|maj|major)?)",
    "scale": r"(?P<scale>.+?)",
    "key_or_chord": r"(?P<key_or_chord>.+?)",
    "author_code": r"(?P<author_code>.+?)",
    "pack_code": r"(?P<pack_code>.+?)",
    "resource_type": r"(?P<resource_type>.+?)",
    "resource_subtype": r"(?P<resource_subtype>.+?)",
    "qualifier": r"(?P<qualifier>.+?)",
    "asset_index": r"(?P<asset_index>\d{1,5})",
    "variant": r"(?P<variant>v?\d{1,5})",
    "profile_id": r"(?P<profile_id>.+?)",
}


def _format_bpm(value: float | int | str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not 20 <= number <= 400:
        return ""
    rounded = round(number)
    if abs(number - rounded) < 0.1:
        return str(rounded)
    return f"{number:.2f}".rstrip("0").rstrip(".")


def normalise_bpm(value: float | int | str) -> str:
    """Normalize the tempo vocabulary owned by the sample-pack workflow."""
    return _format_bpm(str(value).removesuffix("BPM").strip())


def normalise_scale(value: str) -> str:
    """Use the compact key form used by sample-pack names, such as ``Am``."""
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(?:\s*(m|min|minor|maj|major))?", text, re.IGNORECASE)
    if not match:
        return text
    root = match.group(1).upper() + match.group(2)
    mode = (match.group(3) or "").casefold()
    return f"{root}m" if mode in {"m", "min", "minor"} else root


def _bpm_from_name(stem: str) -> str:
    source = str(stem or "")
    match = re.search(r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*BPM(?![A-Za-z])", source, re.IGNORECASE)
    if match:
        return _format_bpm(match.group(1))
    match = re.search(r"\(\s*(\d{2,3}(?:\.\d+)?)\s*(?:,|BPM\b|\))", source, re.IGNORECASE)
    if match:
        return _format_bpm(match.group(1))
    for match in re.finditer(r"(?:^|[_\-\s])(\d{2,3})(?=$|[_\-\s])", source):
        value = _format_bpm(match.group(1))
        if value:
            return value
    match = re.search(r"(?:^|[_\-\s])(\d{2,3})$", source)
    return _format_bpm(match.group(1)) if match else ""


def _read_riff_bpm(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            header = stream.read(12)
            if len(header) < 12 or header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
                return ""
            while True:
                chunk_header = stream.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_id, size = chunk_header[:4], struct.unpack("<I", chunk_header[4:])[0]
                if chunk_id == b"acid" and size >= 24:
                    data = stream.read(24)
                    if len(data) >= 24:
                        bpm = _format_bpm(struct.unpack_from("<f", data, 20)[0])
                        if bpm:
                            return bpm
                    stream.seek(max(0, size - 24), 1)
                elif chunk_id == b"LIST" and size <= 1024 * 1024:
                    data = stream.read(size)
                    if data[:4] == b"INFO":
                        offset = 4
                        while offset + 8 <= len(data):
                            key = data[offset:offset + 4].decode("ascii", "ignore").casefold()
                            length = struct.unpack_from("<I", data, offset + 4)[0]
                            value = data[offset + 8:offset + 8 + length].split(b"\0", 1)[0].decode("utf-8", "ignore").strip()
                            if key in {"bpm ", "tbpm", "ibpm", "temp", "tempo"}:
                                bpm = _format_bpm(value)
                                if bpm:
                                    return bpm
                            offset += 8 + length + (length & 1)
                else:
                    stream.seek(size, 1)
                if size & 1:
                    stream.seek(1, 1)
    except (OSError, struct.error, ValueError):
        return ""
    return ""


def _read_flac_bpm(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"fLaC":
                return ""
            while True:
                block_header = stream.read(4)
                if len(block_header) < 4:
                    break
                last = bool(block_header[0] & 0x80)
                block_type = block_header[0] & 0x7F
                size = int.from_bytes(block_header[1:4], "big")
                data = stream.read(size)
                if block_type == 4 and len(data) >= 8:
                    vendor_size = struct.unpack_from("<I", data, 0)[0]
                    offset = 4 + vendor_size
                    if offset + 4 <= len(data):
                        count = struct.unpack_from("<I", data, offset)[0]
                        offset += 4
                        for _ in range(count):
                            if offset + 4 > len(data):
                                break
                            length = struct.unpack_from("<I", data, offset)[0]
                            offset += 4
                            item = data[offset:offset + length].decode("utf-8", "ignore")
                            offset += length
                            if "=" in item:
                                key, value = item.split("=", 1)
                                if key.casefold() in {"bpm", "tempo", "tbpm"}:
                                    bpm = _format_bpm(value)
                                    if bpm:
                                        return bpm
                if last:
                    break
    except (OSError, struct.error, ValueError):
        return ""
    return ""


def _read_midi_bpm(path: Path) -> str:
    try:
        data = path.read_bytes()
        if len(data) < 14 or data[:4] != b"MThd":
            return ""
        header_size = struct.unpack_from(">I", data, 4)[0]
        track_count = struct.unpack_from(">H", data, 10)[0]
        division = struct.unpack_from(">H", data, 12)[0]
        if division & 0x8000 or not track_count:
            return ""
        offset = 8 + header_size
        tempos: list[tuple[int, int]] = []
        for _ in range(track_count):
            if offset + 8 > len(data) or data[offset:offset + 4] != b"MTrk":
                break
            size = struct.unpack_from(">I", data, offset + 4)[0]
            track_end = min(len(data), offset + 8 + size)
            cursor, absolute_tick = offset + 8, 0
            running_status = 0
            while cursor < track_end:
                delta = 0
                while cursor < track_end:
                    byte = data[cursor]
                    cursor += 1
                    delta = (delta << 7) | (byte & 0x7F)
                    if not byte & 0x80:
                        break
                absolute_tick += delta
                if cursor >= track_end:
                    break
                status = data[cursor]
                if status & 0x80:
                    cursor += 1
                    running_status = status
                else:
                    status = running_status
                if status == 0xFF:
                    if cursor >= track_end:
                        break
                    meta_type = data[cursor]
                    cursor += 1
                    length = 0
                    while cursor < track_end:
                        byte = data[cursor]
                        cursor += 1
                        length = (length << 7) | (byte & 0x7F)
                        if not byte & 0x80:
                            break
                    payload = data[cursor:min(track_end, cursor + length)]
                    cursor += length
                    if meta_type == 0x51 and len(payload) == 3:
                        tempos.append((absolute_tick, int.from_bytes(payload, "big")))
                    if meta_type == 0x2F:
                        break
                elif status in {0xF0, 0xF7}:
                    length = 0
                    while cursor < track_end:
                        byte = data[cursor]
                        cursor += 1
                        length = (length << 7) | (byte & 0x7F)
                        if not byte & 0x80:
                            break
                    cursor += length
                else:
                    cursor += 2 if status & 0xE0 == 0xC0 else 3
            offset = offset + 8 + size
        if tempos:
            _, microseconds = sorted(tempos, key=lambda item: (item[0], item[1]))[0]
            return _format_bpm(60_000_000 / microseconds)
    except (OSError, struct.error, ValueError, ZeroDivisionError):
        return ""
    return ""


def detect_bpm(path: str | Path, stem: str | None = None) -> tuple[str, str]:
    """Read tempo metadata for this workflow, then use its filename fallback."""
    source_path = Path(path)
    try:
        with source_path.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        header = b""
    if header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE":
        bpm = _read_riff_bpm(source_path)
    elif header[:4] == b"fLaC":
        bpm = _read_flac_bpm(source_path)
    elif header[:4] == b"MThd":
        bpm = _read_midi_bpm(source_path)
    else:
        bpm = ""
    if bpm:
        return bpm, "metadata"
    fallback = _bpm_from_name(stem if stem is not None else source_path.stem)
    return (fallback, "name") if fallback else ("", "")


_NOTE = r"[A-Ga-g](?:#|b)?(?:m|min|minor|maj|major|dim|aug|sus|add)?(?:\d{1,2})?(?:add\d{1,2})?"
_KEY_OR_CHORD = rf"(?:{_NOTE}(?:[-_]{_NOTE})+|{_NOTE}\({_NOTE}\)|{_NOTE}_over_{_NOTE}|{_NOTE})"


def _key_or_chord_from_name(stem: str) -> str:
    matches = list(re.finditer(rf"(?:^|_)(?P<value>{_KEY_OR_CHORD})(?=$|_)", stem or "", re.IGNORECASE))
    return matches[-1].group("value") if matches else ""


def _parse_profile_filename(stem: str, workflow: dict[str, Any]) -> dict[str, Any] | None:
    profiles = sorted(workflow.get("profiles", []), key=lambda item: int(item.get("priority", 0)), reverse=True)
    for profile in profiles:
        for pattern in profile.get("parse_patterns", []):
            match = re.fullmatch(pattern, stem, re.IGNORECASE)
            if not match:
                continue
            fields = {
                field_id: str(value)
                for field_id, value in match.groupdict().items()
                if value is not None and str(value).strip()
            }
            fields[workflow.get("profile_field", "profile_id")] = profile["id"]
            if fields.get("bpm"):
                fields["bpm"] = normalise_bpm(fields["bpm"])
            recognized = sum(bool(value) for value in fields.values())
            return {
                "stem": stem,
                "fields": fields,
                "unmatched": "",
                "confidence": min(1.0, recognized / max(1, len(profile.get("ordered_segments", [])))),
                "matched": True,
                "template": profile["id"],
                "field_order": list(fields),
            }
    return None


def parse_filename(stem: str, template: str = "auto",
                   workflow: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse sample-pack naming tokens owned by this workflow.

    The generic parser remains in ``namer_core``.  This adapter adds the
    sample-pack-only BPM/key patterns and removes those tokens before asking
    the generic parser to split the remaining name.
    """
    from namer_core import parse_filename as parse_generic_filename

    if template and template.strip().casefold() not in {"auto", "自动"}:
        return parse_generic_filename(stem, template, _FILENAME_FIELD_PATTERNS)

    source = str(stem or "")
    if workflow:
        profiled = _parse_profile_filename(source, workflow)
        if profiled is not None:
            return profiled
    working = source
    fields: dict[str, str] = {}
    bpm_match = re.search(r"(?<!\d)(\d{2,3})\s*BPM(?![A-Za-z])", working, re.IGNORECASE)
    if not bpm_match:
        bpm_match = next((match for match in re.finditer(r"(?:^|[_\-\s])(\d{2,3})(?=$|[_\-\s])", working)
                          if _format_bpm(match.group(1))), None)
    if bpm_match:
        fields["bpm"] = bpm_match.group(1)
        working = (working[:bpm_match.start()] + working[bpm_match.end():]).strip(" _-.,")
    key_or_chord = _key_or_chord_from_name(working)
    if key_or_chord:
        fields["key_or_chord"] = key_or_chord
        fields["key"] = key_or_chord
        key_start = working.rfind(key_or_chord)
        working = (working[:key_start] + working[key_start + len(key_or_chord):]).strip(" _-.,")
    generic = parse_generic_filename(working)
    fields.update(generic.get("fields", {}))
    if fields.get("type") and not fields.get("resource_type"):
        fields["resource_type"] = fields.pop("type")
    if fields.get("number") and not fields.get("variant"):
        fields["variant"] = fields.pop("number")
    if workflow and workflow.get("default_profile"):
        fields.setdefault(workflow.get("profile_field", "profile_id"), str(workflow["default_profile"]))
    recognized = sum(bool(value) for value in fields.values())
    confidence = min(1.0, recognized / 4.0) if source else 0.0
    return {
        "stem": source,
        "fields": fields,
        "unmatched": generic.get("unmatched", ""),
        "confidence": confidence,
        "matched": bool(fields),
        "template": "auto",
        "field_order": list(fields),
    }


def read_metadata(path: str | Path, _root: str | Path | None = None,
                  _options: dict[str, Any] | None = None) -> dict[str, Any]:
    bpm, bpm_source = detect_bpm(path)
    filename_bpm = _bpm_from_name(Path(path).stem)
    key_or_chord = _key_or_chord_from_name(Path(path).stem)
    values = {}
    if bpm:
        if bpm_source == "metadata":
            values["bpm_metadata"] = bpm
        values.update({"bpm": filename_bpm or bpm, "bpm_source": "name" if filename_bpm else bpm_source})
    if key_or_chord:
        values["key_or_chord"] = key_or_chord
        values["scale"] = normalise_scale(key_or_chord)
    return {"sample_pack": values} if values else {}


def run(request: dict[str, Any]) -> dict[str, Any]:
    """Return BPM suggestions through the generic module result contract."""
    items: list[dict[str, Any]] = []
    for item in request.get("items", []):
        metadata = read_metadata(str(item.get("path", "")))
        sample_pack = metadata.get("sample_pack", {})
        items.append({
            "id": str(item.get("id", "")),
            "values": {
                "bpm_tag": str(sample_pack.get("bpm", "") or ""),
                "tempo_tag": f"Tempo_{sample_pack['bpm']}" if sample_pack.get("bpm") else "",
            },
        })
    return {"items": items}


def append_bpm_suffix(name: str, bpm: str, separator: str = "_") -> str:
    """Legacy-named helper kept inside the sample-pack module."""
    base = str(name or "")
    value = _format_bpm(bpm)
    if not value:
        return base
    if re.search(r"\d{2,3}(?:\.\d+)?\s*BPM$", base, re.IGNORECASE):
        return base
    joiner = str(separator)
    bare_bpm = re.search(rf"(?:^|[_\-\s]){re.escape(value)}$", base)
    if bare_bpm:
        prefix = base[:bare_bpm.start()]
        return f"{prefix}{joiner if joiner else ''}{value}BPM" if prefix else f"{value}BPM"
    return f"{base}{joiner if joiner else ''}{value}BPM" if base else f"{value}BPM"


def wav_duration(path: str | Path) -> float:
    """Read WAV duration for the legacy sample-pack preview panel."""
    try:
        with wave.open(str(path), "rb") as stream:
            return stream.getnframes() / (stream.getframerate() or 1)
    except (OSError, wave.Error):
        return 0.0
