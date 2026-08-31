"""Compatibility facade for the dynamically loaded wallpaper parser."""

from ._compat import load_legacy_module


_implementation = load_legacy_module("wallpaper-assets", "wallpaper")

parse_filename = _implementation.parse_filename
