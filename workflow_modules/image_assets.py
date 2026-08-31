"""Compatibility facade for the dynamically loaded image metadata module."""

from ._compat import load_legacy_module


_implementation = load_legacy_module("wallpaper-assets", "image_assets")

read_image_dimensions = _implementation.read_image_dimensions
read_metadata = _implementation.read_metadata
