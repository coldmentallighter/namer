"""Filename parsing helpers for wallpaper naming workflows."""

from __future__ import annotations

import re
from typing import Any


# Only retain source names that are recognizable providers. Camera and editor
# prefixes such as IMG_ and DSC_ remain manual values instead of becoming
# accidental source labels.
_KNOWN_SOURCES = {
    "artstation", "behance", "bing", "deviantart", "dribbble", "pixiv",
    "pexels", "reddit", "unsplash", "wallhaven",
}
_SOURCE_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9]{1,31})[-_]")


def parse_filename(stem: str, template: str = "auto") -> dict[str, Any]:
    """Return a source candidate for known wallpaper provider prefixes."""
    source = str(stem or "")
    fields: dict[str, str] = {}
    match = _SOURCE_PREFIX.match(source)
    if match:
        candidate = match.group(1).casefold()
        if candidate in _KNOWN_SOURCES:
            fields["source"] = candidate
    return {
        "stem": source,
        "fields": fields,
        "unmatched": "",
        "confidence": 1.0 if fields else 0.0,
        "matched": bool(fields),
        "template": "auto",
        "field_order": list(fields),
    }
