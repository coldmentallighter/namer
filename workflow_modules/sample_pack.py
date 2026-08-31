"""Compatibility facade for the dynamically loaded sample-pack module."""

from ._compat import load_legacy_module


_implementation = load_legacy_module("sample-pack", "sample_pack")

append_bpm_suffix = _implementation.append_bpm_suffix
detect_bpm = _implementation.detect_bpm
normalise_bpm = _implementation.normalise_bpm
normalise_scale = _implementation.normalise_scale
parse_filename = _implementation.parse_filename
read_metadata = _implementation.read_metadata
wav_duration = _implementation.wav_duration
