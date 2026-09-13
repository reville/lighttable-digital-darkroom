# SPDX-License-Identifier: GPL-3.0-only
"""A minimal DNG camera-profile (``.dcp``) writer.

A profile is a TIFF file whose single IFD carries only metadata tags: no
image strips, no tiles. This module assembles exactly that, from plain
Python values, so the bundled LightTable look can be generated from stated
numbers and the reader can be tested against files whose contents are known
to the byte. It writes nothing but what it is given; there is no defaulting
of tags a caller leaves out.

Tag layouts follow the DNG specification. Grids are written value slowest,
then hue, then saturation fastest, which is the order ``camera_profile``
reads them back in.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

import camera_profile as tags_module

ASCII = 2
SHORT = 3
LONG = 4
SRATIONAL = 10
FLOAT = 11

TAG_UNIQUE_CAMERA_MODEL = tags_module.TAG_UNIQUE_CAMERA_MODEL


def ascii_tag(text: str) -> tuple[int, int, bytes]:
    payload = text.encode("utf-8") + b"\x00"
    return (ASCII, len(payload), payload)


def short_tag(values, byteorder: str = "<") -> tuple[int, int, bytes]:
    values = list(values)
    return (SHORT, len(values),
            struct.pack(f"{byteorder}{len(values)}H", *values))


def long_tag(values, byteorder: str = "<") -> tuple[int, int, bytes]:
    values = list(values)
    return (LONG, len(values),
            struct.pack(f"{byteorder}{len(values)}I", *values))


def float_tag(values, byteorder: str = "<") -> tuple[int, int, bytes]:
    array = np.asarray(list(values), dtype=f"{byteorder}f4")
    return (FLOAT, int(array.size), array.tobytes())


def srational_tag(values, byteorder: str = "<",
                  denominator: int = 1000000) -> tuple[int, int, bytes]:
    values = list(values)
    pairs: list[int] = []
    for value in values:
        pairs.extend((int(round(value * denominator)), denominator))
    return (SRATIONAL, len(values),
            struct.pack(f"{byteorder}{len(pairs)}i", *pairs))


def build_tiff(tags: dict, byteorder: str = "<") -> bytes:
    """Assemble a TIFF header plus one IFD holding only ``tags``.

    ``tags`` maps a tag code to a ``(type, count, payload)`` triple. The IFD
    has no strip offsets and no byte counts, exactly like a camera profile.
    """
    magic = b"II" if byteorder == "<" else b"MM"
    header = struct.pack(f"{byteorder}2sHI", magic, 42, 8)
    entries = sorted(tags.items())
    data_offset = 8 + 2 + len(entries) * 12 + 4
    directory = b""
    blobs = b""
    for code, (kind, count, payload) in entries:
        if len(payload) <= 4:
            value = payload + b"\x00" * (4 - len(payload))
        else:
            value = struct.pack(f"{byteorder}I", data_offset + len(blobs))
            blobs += payload
            if len(blobs) % 2:
                blobs += b"\x00"
        directory += struct.pack(f"{byteorder}HHI", code, kind, count) + value
    return (header
            + struct.pack(f"{byteorder}H", len(entries))
            + directory
            + struct.pack(f"{byteorder}I", 0)
            + blobs)


def grid_bytes(hue_divisions: int, sat_divisions: int, value_divisions: int,
               triple, byteorder: str = "<"):
    """Build a hue/sat grid, value slowest then hue then saturation fastest.

    ``triple`` is called with (hue index, sat index, value index) and returns
    (hue shift in degrees, saturation scale, value scale).
    """
    values: list[float] = []
    for value_index in range(value_divisions):
        for hue_index in range(hue_divisions):
            for sat_index in range(sat_divisions):
                values.extend(triple(hue_index, sat_index, value_index))
    return float_tag(values, byteorder)


def grid_from_array(grid, byteorder: str = "<"):
    """Serialise an already shaped (value, hue, sat, 3) grid."""
    return float_tag(np.asarray(grid, dtype=np.float64).ravel(), byteorder)


T = tags_module


def profile_tags(*, name="Test Profile", copyright_text=None,
                 camera_model=None,
                 hue_sat_dims=None, hue_sat_map=None, hue_sat_map_2=None,
                 look_dims=None, look_table=None, tone_curve=None,
                 illuminant1=17, illuminant2=None, colour_matrix=None,
                 colour_matrix_2=None, forward_matrix=None,
                 forward_matrix_2=None, embed_policy=1, signature=None,
                 hue_sat_encoding=None, look_encoding=None,
                 baseline_exposure_offset=None, default_black_render=None,
                 byteorder="<") -> dict:
    """The tag table for one profile; every argument is optional."""
    tags: dict = {}
    if name is not None:
        tags[T.TAG_PROFILE_NAME] = ascii_tag(name)
    if copyright_text is not None:
        tags[T.TAG_PROFILE_COPYRIGHT] = ascii_tag(copyright_text)
    if camera_model is not None:
        tags[T.TAG_UNIQUE_CAMERA_MODEL] = ascii_tag(camera_model)
    if signature is not None:
        tags[T.TAG_PROFILE_CALIBRATION_SIGNATURE] = ascii_tag(signature)
    if hue_sat_dims is not None:
        tags[T.TAG_PROFILE_HUE_SAT_MAP_DIMS] = long_tag(
            hue_sat_dims, byteorder)
    if hue_sat_map is not None:
        tags[T.TAG_PROFILE_HUE_SAT_MAP_DATA_1] = hue_sat_map
    if hue_sat_map_2 is not None:
        tags[T.TAG_PROFILE_HUE_SAT_MAP_DATA_2] = hue_sat_map_2
    if look_dims is not None:
        tags[T.TAG_PROFILE_LOOK_TABLE_DIMS] = long_tag(look_dims, byteorder)
    if look_table is not None:
        tags[T.TAG_PROFILE_LOOK_TABLE_DATA] = look_table
    if tone_curve is not None:
        tags[T.TAG_PROFILE_TONE_CURVE] = float_tag(
            np.asarray(tone_curve, dtype=np.float64).ravel(), byteorder)
    if illuminant1 is not None:
        tags[T.TAG_CALIBRATION_ILLUMINANT_1] = short_tag(
            [illuminant1], byteorder)
    if illuminant2 is not None:
        tags[T.TAG_CALIBRATION_ILLUMINANT_2] = short_tag(
            [illuminant2], byteorder)
    if colour_matrix is not None:
        tags[T.TAG_COLOR_MATRIX_1] = srational_tag(
            np.asarray(colour_matrix, dtype=np.float64).ravel(), byteorder)
    if colour_matrix_2 is not None:
        tags[T.TAG_COLOR_MATRIX_2] = srational_tag(
            np.asarray(colour_matrix_2, dtype=np.float64).ravel(), byteorder)
    if forward_matrix is not None:
        tags[T.TAG_FORWARD_MATRIX_1] = srational_tag(
            np.asarray(forward_matrix, dtype=np.float64).ravel(), byteorder)
    if forward_matrix_2 is not None:
        tags[T.TAG_FORWARD_MATRIX_2] = srational_tag(
            np.asarray(forward_matrix_2, dtype=np.float64).ravel(), byteorder)
    if embed_policy is not None:
        tags[T.TAG_PROFILE_EMBED_POLICY] = long_tag([embed_policy], byteorder)
    if hue_sat_encoding is not None:
        tags[T.TAG_PROFILE_HUE_SAT_MAP_ENCODING] = long_tag(
            [hue_sat_encoding], byteorder)
    if look_encoding is not None:
        tags[T.TAG_PROFILE_LOOK_TABLE_ENCODING] = long_tag(
            [look_encoding], byteorder)
    if baseline_exposure_offset is not None:
        tags[T.TAG_BASELINE_EXPOSURE_OFFSET] = srational_tag(
            [baseline_exposure_offset], byteorder)
    if default_black_render is not None:
        tags[T.TAG_DEFAULT_BLACK_RENDER] = long_tag(
            [default_black_render], byteorder)
    return tags


def write_profile(path, byteorder: str = "<", **kwargs) -> Path:
    """Write one profile and return its path."""
    path = Path(path)
    path.write_bytes(build_tiff(
        profile_tags(byteorder=byteorder, **kwargs), byteorder))
    return path
