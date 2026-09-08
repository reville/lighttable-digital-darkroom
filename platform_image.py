"""Platform image services with the existing macOS path kept as the fast path."""

from __future__ import annotations

import ctypes
import ctypes.util
import io
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

import durable_io


PROFILE_FILENAMES = {
    "srgb": "sRGB-v4.icc",
    "display_p3": "DisplayP3-v4.icc",
    "prophoto": "ProPhoto-v4.icc",
}

MACOS_PROFILE_PATHS = {
    "srgb": Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc"),
    "display_p3": Path("/System/Library/ColorSync/Profiles/Display P3.icc"),
    "prophoto": Path("/System/Library/ColorSync/Profiles/ROMM RGB.icc"),
}

EXIF_FIELDS = {
    # `Make` matters beyond the info panel: it is half of the per-camera
    # defaults key and half of the lensfun camera match. It was declared in a
    # second, unused list in server.py while this one omitted it, so every
    # camera resolved with an empty make.
    "Make": ("Exif.Image.Make",),
    "Model": ("Exif.Image.Model",),
    "BodySerialNumber": ("Exif.Photo.BodySerialNumber",
                         "Exif.Image.CameraSerialNumber"),
    "LensModel": ("Exif.Photo.LensModel",),
    "LensID": ("Exif.Photo.LensModel",),
    "FocalLength": ("Exif.Photo.FocalLength",),
    "FNumber": ("Exif.Photo.FNumber",),
    "ExposureTime": ("Exif.Photo.ExposureTime",),
    "ISO": ("Exif.Photo.PhotographicSensitivity", "Exif.Photo.ISOSpeedRatings"),
    "DateTimeOriginal": ("Exif.Photo.DateTimeOriginal",),
    "OffsetTimeOriginal": ("Exif.Photo.OffsetTimeOriginal",),
    "ImageWidth": ("Exif.Photo.PixelXDimension", "Exif.Image.ImageWidth"),
    "ImageHeight": ("Exif.Photo.PixelYDimension", "Exif.Image.ImageLength"),
    "FocusDistance": ("Exif.Photo.SubjectDistance",),
}


def profile_path(app_root: Path, output_space: str) -> Path:
    """Return the native profile on macOS and the bundled profile elsewhere."""
    output_space = output_space if output_space in PROFILE_FILENAMES else "srgb"
    bundled = Path(app_root) / "color-profiles" / PROFILE_FILENAMES[output_space]
    if sys.platform == "darwin":
        native = MACOS_PROFILE_PATHS[output_space]
        if native.is_file():
            return native
    return bundled


def _use_macos_tools(force_portable: bool) -> bool:
    return (
        not force_portable
        and sys.platform == "darwin"
        and shutil.which("sips") is not None
    )


@lru_cache(maxsize=1)
def _imageio_frameworks():
    """Load the small CoreFoundation/ImageIO ABI used for thumbnails."""
    if sys.platform != "darwin":
        raise RuntimeError("ImageIO is only available on macOS")
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    imageio = ctypes.CDLL(
        "/System/Library/Frameworks/ImageIO.framework/ImageIO")
    core.CFURLCreateFromFileSystemRepresentation.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
    core.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    core.CFNumberCreate.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    core.CFNumberCreate.restype = ctypes.c_void_p
    core.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    core.CFStringCreateWithCString.restype = ctypes.c_void_p
    core.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
        ctypes.c_void_p, ctypes.c_void_p]
    core.CFDictionaryCreate.restype = ctypes.c_void_p
    core.CFRelease.argtypes = [ctypes.c_void_p]
    imageio.CGImageSourceCreateWithURL.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p]
    imageio.CGImageSourceCreateWithURL.restype = ctypes.c_void_p
    imageio.CGImageSourceCreateThumbnailAtIndex.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    imageio.CGImageSourceCreateThumbnailAtIndex.restype = ctypes.c_void_p
    imageio.CGImageDestinationCreateWithURL.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    imageio.CGImageDestinationCreateWithURL.restype = ctypes.c_void_p
    imageio.CGImageDestinationAddImage.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    imageio.CGImageDestinationFinalize.argtypes = [ctypes.c_void_p]
    imageio.CGImageDestinationFinalize.restype = ctypes.c_bool
    return core, imageio


def _cf_symbol(library, name: str) -> int:
    return int(ctypes.c_void_p.in_dll(library, name).value or 0)


def _build_thumbnail_imageio(source: Path, destination: Path,
                             max_pixel: int = 240,
                             quality: float = 0.8, *, output_type: str = "public.jpeg",
                             from_full_image: bool = False) -> None:
    """Decode an oriented thumbnail in-process through macOS ImageIO.

    ``CGImageSourceCreateThumbnailAtIndex`` asks ImageIO for the bounded draft
    directly, allowing it to use an embedded preview or a hardware decoder
    without inflating the full source into Python memory.
    """
    core, imageio = _imageio_frameworks()
    owned: list[int] = []

    def own(value, label: str) -> int:
        pointer = int(value or 0)
        if not pointer:
            raise RuntimeError(f"ImageIO could not create {label}")
        owned.append(pointer)
        return pointer

    def file_url(path: Path) -> int:
        encoded = os.fsencode(path)
        return own(core.CFURLCreateFromFileSystemRepresentation(
            None, encoded, len(encoded), False), "file URL")

    try:
        source_ref = own(imageio.CGImageSourceCreateWithURL(
            file_url(source), None), "image source")
        maximum = ctypes.c_int32(max(1, int(max_pixel)))
        maximum_ref = own(core.CFNumberCreate(
            None, 3, ctypes.byref(maximum)), "thumbnail size")
        keys = (ctypes.c_void_p * 3)(
            _cf_symbol(imageio,
                       "kCGImageSourceCreateThumbnailFromImageAlways" if from_full_image else
                       "kCGImageSourceCreateThumbnailFromImageIfAbsent"),
            _cf_symbol(imageio, "kCGImageSourceCreateThumbnailWithTransform"),
            _cf_symbol(imageio, "kCGImageSourceThumbnailMaxPixelSize"),
        )
        true_ref = _cf_symbol(core, "kCFBooleanTrue")
        values = (ctypes.c_void_p * 3)(true_ref, true_ref, maximum_ref)
        options = own(core.CFDictionaryCreate(
            None, keys, values, 3, None, None), "thumbnail options")
        thumbnail = own(imageio.CGImageSourceCreateThumbnailAtIndex(
            source_ref, 0, options), "thumbnail")
        jpeg_type = own(core.CFStringCreateWithCString(
            None, output_type.encode("ascii"), 0x08000100), "JPEG type")
        output = own(imageio.CGImageDestinationCreateWithURL(
            file_url(destination), jpeg_type, 1, None), "image destination")
        compression = ctypes.c_float(max(0.0, min(1.0, float(quality))))
        compression_ref = own(core.CFNumberCreate(
            None, 5, ctypes.byref(compression)), "JPEG quality")
        property_keys = (ctypes.c_void_p * 1)(
            _cf_symbol(imageio,
                       "kCGImageDestinationLossyCompressionQuality"))
        property_values = (ctypes.c_void_p * 1)(compression_ref)
        properties = own(core.CFDictionaryCreate(
            None, property_keys, property_values, 1, None, None),
            "JPEG properties")
        imageio.CGImageDestinationAddImage(output, thumbnail, properties)
        if not imageio.CGImageDestinationFinalize(output):
            raise RuntimeError("ImageIO could not finalize the thumbnail")
    finally:
        for pointer in reversed(owned):
            core.CFRelease(pointer)


def embed_jpeg_icc(destination: Path | str, profile: bytes) -> None:
    """Insert an ICC profile into an already-encoded JPEG without re-encoding.

    The resident Rust exporter owns JPEG compression. ICC APP2 segments are a
    container operation, so adding them here keeps its pixels byte-for-byte
    intact while preserving the color-management contract of Python exports.
    """
    path = Path(destination)
    payload = path.read_bytes()
    if not payload.startswith(b"\xff\xd8"):
        raise ValueError("ICC profile target is not a JPEG")
    if not profile:
        return
    identifier = b"ICC_PROFILE\x00"
    maximum_chunk = 65533 - len(identifier) - 2
    chunks = [profile[offset:offset + maximum_chunk]
              for offset in range(0, len(profile), maximum_chunk)]
    if len(chunks) > 255:
        raise ValueError("ICC profile needs more than 255 JPEG segments")
    segments = []
    for sequence, chunk in enumerate(chunks, 1):
        body = identifier + bytes((sequence, len(chunks))) + chunk
        segments.append(b"\xff\xe2" + (len(body) + 2).to_bytes(2, "big") + body)
    path.write_bytes(payload[:2] + b"".join(segments) + payload[2:])


def _exiv2_value(data, *keys: str) -> str:
    import exiv2

    for key in keys:
        try:
            lookup = exiv2.ExifKey(key)
        except exiv2.Exiv2Error as error:
            if error.code != exiv2.ErrorCode.kerInvalidTag:
                raise
            # Exiv2 versions do not all recognize the same aliases. Continue
            # to the supported fallback instead of discarding every EXIF field.
            continue
        item = data.findKey(lookup)
        if item != data.end():
            return item.toString()
    return ""


def orientation_degrees(source: Path, *, force_portable: bool = False) -> int:
    """Return the clockwise EXIF orientation rotation for one image."""
    source = Path(source)
    orientation = 1
    if _use_macos_tools(force_portable) and shutil.which("exiftool"):
        try:
            result = subprocess.run(
                ["exiftool", "-n", "-s3", "-Orientation", str(source)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            orientation = int((result.stdout or "1").strip() or 1)
        except (OSError, subprocess.SubprocessError, ValueError):
            orientation = 1
    else:
        try:
            import exiv2

            image = exiv2.ImageFactory.open(str(source))
            image.readMetadata()
            raw = _exiv2_value(image.exifData(), "Exif.Image.Orientation")
            orientation = int(raw or 1)
        except Exception:  # noqa: BLE001 - malformed metadata is non-fatal
            orientation = 1
    return {3: 180, 6: 90, 8: 270}.get(orientation, 0)


def metadata(source: Path, *, force_portable: bool = False) -> dict[str, str]:
    """Read the small metadata subset displayed by the editor."""
    source = Path(source)
    if _use_macos_tools(force_portable) and shutil.which("exiftool"):
        fields = list(EXIF_FIELDS) + ["FileSize"]
        try:
            result = subprocess.run(
                ["exiftool", "-S", *[f"-{field}" for field in fields], str(source)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            output = {}
            for line in result.stdout.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    output[key.strip()] = value.strip()
            return output
        except (OSError, subprocess.SubprocessError):
            return {}

    output: dict[str, str] = {}
    try:
        import exiv2

        image = exiv2.ImageFactory.open(str(source))
        image.readMetadata()
        data = image.exifData()
        output.update({
            field: value
            for field, keys in EXIF_FIELDS.items()
            if (value := _exiv2_value(data, *keys))
        })
    except Exception:  # noqa: BLE001 - metadata must not block browsing
        pass
    try:
        with Image.open(source) as image:
            exif = image.getexif()
            pillow_fields = {
                "Model": 272,
                "ExposureTime": 33434,
                "FNumber": 33437,
                "ISO": 34855,
                "DateTimeOriginal": 36867,
                "FocalLength": 37386,
                "LensModel": 42036,
                "ImageWidth": 40962,
                "ImageHeight": 40963,
            }
            for field, tag in pillow_fields.items():
                if field not in output and exif.get(tag) is not None:
                    output[field] = str(exif[tag])
    except Exception:  # noqa: BLE001 - broad-format metadata may be exiv2-only
        pass
    try:
        output["FileSize"] = f"{source.stat().st_size} bytes"
    except OSError:
        pass
    return output


def _open_portable(source: Path) -> tuple[Image.Image, bytes | None]:
    """Open through Pillow first, then the existing broad-format runtime."""
    try:
        image = Image.open(source)
        image.load()
        embedded = image.info.get("icc_profile")
        return ImageOps.exif_transpose(image).convert("RGB"), embedded
    except Exception as pillow_error:  # noqa: BLE001 - try the broad decoder
        try:
            import OpenImageIO as oiio

            buffer = oiio.ImageBuf(str(source))
            spec = buffer.spec()
            if spec.width <= 0 or spec.height <= 0:
                raise ValueError(buffer.geterror() or "image decoder returned no pixels")
            pixels = np.asarray(buffer.get_pixels(oiio.UINT8))
            if pixels.ndim != 3 or pixels.shape[2] < 3:
                raise ValueError("image decoder did not return RGB pixels")
            attribute = spec.getattribute("ICCProfile")
            embedded = bytes(attribute) if attribute is not None else None
            return Image.fromarray(pixels[..., :3], "RGB"), embedded
        except Exception as broad_error:  # noqa: BLE001
            raise RuntimeError(
                f"could not decode {source.name}: {broad_error}"
            ) from pillow_error


def _convert_profile(
    image: Image.Image,
    embedded_profile: bytes | None,
    app_root: Path,
    output_space: str,
) -> tuple[Image.Image, bytes | None]:
    target_path = profile_path(app_root, output_space)
    try:
        target_bytes = target_path.read_bytes()
    except OSError:
        return image, None

    source_bytes = embedded_profile
    if not source_bytes:
        try:
            source_bytes = profile_path(app_root, "srgb").read_bytes()
        except OSError:
            source_bytes = None
    if not source_bytes:
        return image, target_bytes

    converted = ImageCms.profileToProfile(
        image,
        ImageCms.ImageCmsProfile(io.BytesIO(source_bytes)),
        ImageCms.ImageCmsProfile(io.BytesIO(target_bytes)),
        outputMode="RGB",
        renderingIntent=ImageCms.Intent.PERCEPTUAL,
    )
    return converted, target_bytes


@lru_cache(maxsize=1)
def _littlecms():
    """The stable LittleCMS 2 float-pixel ABI used by Linux TIFF imports."""
    library = ctypes.util.find_library("lcms2")
    if not library:
        raise RuntimeError("High-precision TIFF color conversion needs LittleCMS 2 (liblcms2)")
    cms = ctypes.CDLL(library)
    pointer, uint = ctypes.c_void_p, ctypes.c_uint32
    cms.cmsOpenProfileFromMem.argtypes = [pointer, uint]
    cms.cmsOpenProfileFromMem.restype = pointer
    cms.cmsCloseProfile.argtypes = [pointer]
    cms.cmsCloseProfile.restype = ctypes.c_int
    cms.cmsCreateTransform.argtypes = [pointer, uint, pointer, uint, uint, uint]
    cms.cmsCreateTransform.restype = pointer
    cms.cmsDoTransform.argtypes = [pointer, pointer, pointer, uint]
    cms.cmsDoTransform.restype = None
    cms.cmsDeleteTransform.argtypes = [pointer]
    cms.cmsDeleteTransform.restype = None
    return cms


def _open_linux_tiff_float(source: Path) -> tuple[np.ndarray, bytes | None]:
    """Decode TIFF samples without Pillow's RGB8 conversion, including LZW."""
    import OpenImageIO as oiio

    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    config.attribute("oiio:reorient", 0)
    reader = oiio.ImageInput.open(str(source), config)
    if reader is None:
        raise ValueError(oiio.geterror() or "could not open TIFF")
    try:
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        if pixels is None or spec.width <= 0 or spec.height <= 0:
            raise ValueError(reader.geterror() or "TIFF decoder returned no pixels")
        pixels = np.asarray(pixels, dtype=np.float32)
        if pixels.ndim != 3 or pixels.shape[2] < 1:
            raise ValueError("TIFF decoder did not return a two-dimensional image")
        channels = 1 if spec.nchannels <= 2 else 3
        rgb = pixels[..., :channels]
        # Match dropping alpha from an unassociated image. OIIO leaves an
        # associated TIFF associated, so recover its color channels first.
        if spec.alpha_channel >= 0 and not spec.get_int_attribute("oiio:UnassociatedAlpha", 0):
            alpha = pixels[..., spec.alpha_channel:spec.alpha_channel + 1]
            rgb = np.divide(rgb, alpha, out=np.zeros_like(rgb), where=alpha > 0)
        orientation = spec.get_int_attribute("Orientation", 1)
        if orientation == 2:
            rgb = rgb[:, ::-1]
        elif orientation == 3:
            rgb = rgb[::-1, ::-1]
        elif orientation == 4:
            rgb = rgb[::-1]
        elif orientation == 5:
            rgb = rgb.transpose(1, 0, 2)
        elif orientation == 6:
            rgb = np.rot90(rgb, -1)
        elif orientation == 7:
            rgb = rgb[::-1, ::-1].transpose(1, 0, 2)
        elif orientation == 8:
            rgb = np.rot90(rgb, 1)
        # ICCProfile is a uint8 array; get_bytes_attribute stringifies arrays.
        profile_attribute = spec.getattribute("ICCProfile")
        embedded = bytes(profile_attribute) if profile_attribute is not None else None
        return np.ascontiguousarray(rgb), embedded
    finally:
        reader.close()


def _convert_float_profile(pixels: np.ndarray, embedded: bytes | None,
                           app_root: Path, output_space: str) -> tuple[np.ndarray, bytes | None]:
    """Convert normalized float RGB/gray through ICC without an RGB8 round-trip."""
    try:
        target = profile_path(app_root, output_space).read_bytes()
    except OSError:
        # Retain the existing missing-profile behavior, without losing samples.
        return (np.repeat(pixels, 3, axis=2) if pixels.shape[2] == 1 else pixels), None
    source = embedded
    if not source:
        try:
            source = profile_path(app_root, "srgb").read_bytes()
        except OSError:
            source = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        if pixels.shape[2] == 1:
            pixels = np.repeat(pixels, 3, axis=2)
    if source == target and pixels.shape[2] == 3:
        return pixels, target
    cms = _littlecms()
    # lcms2.h: FLOAT_SH(1) | COLORSPACE_SH(PT_RGB=4 / PT_GRAY=3)
    #          | CHANNELS_SH(n) | BYTES_SH(4). Perceptual matches Pillow's path.
    rgb_float = (1 << 22) | (4 << 16) | (3 << 3) | 4
    input_float = (1 << 22) | ((3 if pixels.shape[2] == 1 else 4) << 16) | (pixels.shape[2] << 3) | 4
    source_buffer = ctypes.create_string_buffer(source)
    target_buffer = ctypes.create_string_buffer(target)
    input_profile = output_profile = transform = None
    try:
        input_profile = cms.cmsOpenProfileFromMem(source_buffer, len(source))
        output_profile = cms.cmsOpenProfileFromMem(target_buffer, len(target))
        if not input_profile or not output_profile:
            raise ValueError("TIFF color profile could not be read")
        transform = cms.cmsCreateTransform(input_profile, input_float, output_profile,
                                           rgb_float, 0, 0)
        if not transform:
            raise ValueError("TIFF color profile is incompatible with its pixel channels")
        pixels = np.ascontiguousarray(pixels, dtype=np.float32)
        output = np.empty((*pixels.shape[:2], 3), dtype=np.float32)
        # The ABI count is uint32; chunks also bound each native call.
        flat_input, flat_output = pixels.reshape(-1, pixels.shape[2]), output.reshape(-1, 3)
        for start in range(0, len(flat_input), 1_000_000):
            chunk = flat_input[start:start + 1_000_000]
            cms.cmsDoTransform(transform, chunk.ctypes.data,
                               flat_output[start:].ctypes.data, len(chunk))
        return output, target
    finally:
        if transform:
            cms.cmsDeleteTransform(transform)
        if input_profile:
            cms.cmsCloseProfile(input_profile)
        if output_profile:
            cms.cmsCloseProfile(output_profile)


def _write_linux_tiff(destination: Path, pixels: np.ndarray,
                      profile: bytes | None) -> None:
    import tifffile
    # Uncompressed cache TIFFs are readable without optional imagecodecs.
    tags = [(34675, "B", len(profile), profile, False)] if profile else []
    tifffile.imwrite(destination, pixels, photometric="rgb", metadata=None,
                     compression=None, extratags=tags)


def _open_portable_full_precision(source: Path) -> tuple[np.ndarray, bytes | None]:
    """Decode export pixels without Pillow's 8-bit multichannel conversion."""
    import OpenImageIO as oiio

    config = oiio.ImageSpec()
    # Dropping alpha must retain the underlying color, as Pillow RGB does.
    config.attribute("oiio:UnassociatedAlpha", 1)
    config.attribute("oiio:reorient", 0)
    buffer = oiio.ImageBuf(str(source), 0, 0, config)
    spec = buffer.spec()
    if spec.width <= 0 or spec.height <= 0:
        raise RuntimeError(f"could not decode {source.name}: {buffer.geterror()}")
    attribute = spec.getattribute("ICCProfile")
    embedded = bytes(attribute) if attribute is not None else None
    # Integer sources get at least 16-bit precision through the ICC transform;
    # floating-point sources retain their range and fractional precision.
    pixel_type = (oiio.UINT16 if spec.format.basetype in (oiio.UINT8, oiio.UINT16)
                  else oiio.FLOAT)
    pixels = buffer.get_pixels(pixel_type)
    if pixels is None or buffer.has_error:
        raise RuntimeError(f"could not decode {source.name}: {buffer.geterror()}")
    pixels = np.asarray(pixels)
    if pixels.ndim != 3 or pixels.shape[2] == 0:
        raise ValueError("image decoder did not return color pixels")
    # Match EXIF/Pillow orientation, including mirrored orientations 5 and 7.
    orientation = spec.get_int_attribute("Orientation", 1)
    if orientation == 2:
        pixels = pixels[:, ::-1]
    elif orientation == 3:
        pixels = pixels[::-1, ::-1]
    elif orientation == 4:
        pixels = pixels[::-1]
    elif orientation == 5:
        pixels = pixels.transpose(1, 0, 2)
    elif orientation == 6:
        pixels = np.rot90(pixels, -1)
    elif orientation == 7:
        pixels = pixels.transpose(1, 0, 2)[::-1, ::-1]
    elif orientation == 8:
        pixels = np.rot90(pixels, 1)
    if pixels.shape[2] < 3:
        return np.ascontiguousarray(pixels[..., 0]), embedded
    return np.ascontiguousarray(pixels[..., :3]), embedded


def _convert_profile_full_precision(
    pixels: np.ndarray, embedded_profile: bytes | None,
    app_root: Path, output_space: str,
) -> tuple[np.ndarray, bytes]:
    """Apply the same perceptual ICC intent with LittleCMS 16-bit/float I/O."""
    import imagecodecs

    target_bytes = profile_path(app_root, output_space).read_bytes()
    source_bytes = embedded_profile or profile_path(app_root, "srgb").read_bytes()
    # Untagged gray inputs follow the same assumed-sRGB policy as RGB inputs.
    # Tagged gray inputs keep their own tone curve through the ICC transform.
    if pixels.ndim == 2 and imagecodecs.cms_info(source_bytes)["colorspace"] == "rgb":
        pixels = np.repeat(pixels[..., None], 3, axis=2)
    converted = imagecodecs.cms_transform(
        pixels, source_bytes, target_bytes,
        planar=False, outplanar=False, outcolorspace="rgb",
        intent=imagecodecs.CMS.INTENT.PERCEPTUAL,
        # Preserve the profile curves instead of resampling them into a
        # coarse integer lookup table, especially in shadows and at gamut edges.
        flags=imagecodecs.CMS.FLAGS.NOOPTIMIZE,
    )
    return converted, target_bytes


def convert_processed_to_tiff(
    source: Path,
    destination: Path,
    *,
    app_root: Path,
    output_space: str,
    force_portable: bool = False,
) -> None:
    """Convert a processed input while retaining the current native Mac path."""
    source = Path(source)
    destination = Path(destination)
    target = profile_path(app_root, output_space)
    if _use_macos_tools(force_portable):
        command = ["sips", "-s", "format", "tiff"]
        if target.is_file():
            command += ["--matchTo", str(target)]
        command += [str(source), "--out", str(destination)]
        subprocess.run(command, check=True, capture_output=True)
        rotation = orientation_degrees(source)
        if rotation:
            subprocess.run(
                ["sips", "-r", str(rotation), str(destination)],
                check=True,
                capture_output=True,
            )
        return

    if sys.platform.startswith("linux"):
        # Linux uses system LittleCMS float I/O for TIFFs and uncompressed
        # caches that remain readable without the optional imagecodecs wheel.
        if source.suffix.lower() in {".tif", ".tiff"}:
            pixels, embedded = _open_linux_tiff_float(source)
            pixels, profile = _convert_float_profile(pixels, embedded, app_root, output_space)
        else:
            image, embedded = _open_portable(source)
            image, profile = _convert_profile(image, embedded, app_root, output_space)
            pixels = np.asarray(image)
        _write_linux_tiff(destination, pixels, profile)
        return

    import tifffile

    pixels, embedded = _open_portable_full_precision(source)
    pixels, profile = _convert_profile_full_precision(
        pixels, embedded, app_root, output_space)
    tifffile.imwrite(
        destination, pixels, photometric="rgb", metadata=None,
        compression="lzw", iccprofile=profile,
    )


def processed_preview(source: Path, max_width: int, *, app_root: Path,
                      output_space: str = "prophoto") -> np.ndarray:
    """Decode/convert at preview size; keep full-precision TIFFs for export."""
    source = Path(source)
    max_width = max(1, int(max_width))
    if sys.platform.startswith("linux") and source.suffix.lower() in {".tif", ".tiff"}:
        pixels, embedded = _open_linux_tiff_float(source)
        if pixels.shape[1] > max_width:
            size = (max_width, max(1, round(pixels.shape[0] * max_width / pixels.shape[1])))
            pixels = np.stack([np.asarray(Image.fromarray(pixels[..., channel]).resize(
                size, Image.Resampling.LANCZOS), dtype=np.float32)
                for channel in range(pixels.shape[2])], axis=2)
        return _convert_float_profile(pixels, embedded, app_root, output_space)[0]
    try:
        with Image.open(source) as opened:
            embedded = opened.info.get("icc_profile")
            orientation = opened.getexif().get(274, 1)
            oriented_width = opened.height if orientation in (5, 6, 7, 8) else opened.width
            ratio = min(1.0, max_width / max(1, oriented_width))
            opened.draft("RGB", (max(1, round(opened.width * ratio)),
                                 max(1, round(opened.height * ratio))))
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except (OSError, ValueError):
        if sys.platform == "darwin":
            import tempfile
            with tempfile.TemporaryDirectory(prefix="lighttable-preview-") as directory:
                draft = Path(directory) / "preview.tif"
                # ImageIO handles HEIC with bounded native decode. A lossless
                # temporary retains its ICC profile without a JPEG round-trip.
                _build_thumbnail_imageio(source, draft, max_pixel=max_width,
                                         output_type="public.tiff", from_full_image=True)
                image, embedded = _open_portable(draft)
        else:
            image, embedded = _open_portable(source)
    if image.width > max_width:
        image = image.resize((max_width, max(1, round(
            image.height * max_width / image.width))), Image.Resampling.LANCZOS)
    image, _ = _convert_profile(image, embedded, app_root, output_space)
    return np.asarray(image, dtype=np.float32) / 255.0


def build_thumbnail(
    source: Path,
    destination: Path,
    *,
    force_portable: bool = False,
) -> None:
    """Build a small oriented JPEG thumbnail."""
    source = Path(source)
    destination = Path(destination)
    if sys.platform == "darwin" and not force_portable:
        try:
            _build_thumbnail_imageio(source, destination)
            return
        except (OSError, RuntimeError, ValueError):
            # Corrupt or unusually encoded files still get the portable and
            # command-line compatibility paths below.
            pass

    # Pillow's JPEG draft mode decodes close to thumbnail resolution on other
    # platforms and provides the first macOS fallback for malformed metadata.
    if source.suffix.lower() not in {".heic", ".hif"}:
        try:
            with Image.open(source) as opened:
                if source.suffix.lower() in {".jpg", ".jpeg"}:
                    opened.draft("RGB", (240, 240))
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((240, 240), Image.Resampling.LANCZOS)
                image.save(destination, "JPEG", quality=80, subsampling=1)
                return
        except (OSError, ValueError):
            pass
    if _use_macos_tools(force_portable):
        subprocess.run(
            [
                "sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                "-Z", "240", str(source), "--out", str(destination),
            ],
            check=True,
            capture_output=True,
        )
        rotation = orientation_degrees(source)
        if rotation:
            subprocess.run(
                ["sips", "-r", str(rotation), str(destination)],
                check=True,
                capture_output=True,
            )
        return

    image, _ = _open_portable(source)
    image = ImageOps.exif_transpose(image)
    image.thumbnail((240, 240), Image.Resampling.LANCZOS)
    image.save(destination, "JPEG", quality=80, subsampling=1)


METADATA_POLICIES = ("none", "copyright", "all", "all-except-location")

SOFTWARE_TAG = "LightTable"

LIGHTROOM_NAMESPACE = "http://ns.adobe.com/lightroom/1.0/"

# Tags that describe how the destination file is laid out on disk. Copying
# these from the source would mislabel or corrupt the export -- the ICC profile
# of a TIFF lives in Exif.Image.InterColorProfile, so it is on this list too.
_EXIF_STRUCTURE_KEYS = frozenset({
    "Exif.Image.NewSubfileType", "Exif.Image.SubfileType",
    "Exif.Image.ImageWidth", "Exif.Image.ImageLength",
    "Exif.Image.BitsPerSample", "Exif.Image.Compression",
    "Exif.Image.PhotometricInterpretation", "Exif.Image.FillOrder",
    "Exif.Image.StripOffsets", "Exif.Image.SamplesPerPixel",
    "Exif.Image.RowsPerStrip", "Exif.Image.StripByteCounts",
    "Exif.Image.PlanarConfiguration", "Exif.Image.Predictor",
    "Exif.Image.SampleFormat", "Exif.Image.YCbCrSubSampling",
    "Exif.Image.TileWidth", "Exif.Image.TileLength",
    "Exif.Image.TileOffsets", "Exif.Image.TileByteCounts",
    "Exif.Image.JPEGInterchangeFormat",
    "Exif.Image.JPEGInterchangeFormatLength",
    "Exif.Image.InterColorProfile", "Exif.Image.SubIFDs",
    "Exif.Image.ExifTag", "Exif.Image.GPSTag",
    "Exif.Photo.InteroperabilityTag",
})

_EXIF_STRUCTURE_PREFIXES = (
    "Exif.Thumbnail.", "Exif.Image2.", "Exif.Image3.", "Exif.SubImage",
)

_GPS_KEYS = ("Exif.Image.GPSTag",)

_GPS_PREFIX = "Exif.GPSInfo."


def _erase_gps(exif) -> None:
    import exiv2

    doomed = [datum.key() for datum in exif
              if datum.key().startswith(_GPS_PREFIX)
              or datum.key() in _GPS_KEYS]
    for key in doomed:
        position = exif.findKey(exiv2.ExifKey(key))
        if position != exif.end():
            exif.erase(position)


def _copy_source_exif(exif, source_exif, *, keep_location: bool,
                      warnings: list[str] | None = None) -> None:
    for datum in source_exif:
        key = datum.key()
        if (key in _EXIF_STRUCTURE_KEYS
                or key.startswith(_EXIF_STRUCTURE_PREFIXES)):
            continue
        if not keep_location and key.startswith(_GPS_PREFIX):
            continue
        try:
            exif[key] = datum.value()
        except Exception:  # noqa: BLE001 - retain pixels and name the skipped tag
            if warnings is not None:
                warnings.append(f"Metadata tag {key} could not be copied.")


def _xmp_text(xmp, key: str, value) -> None:
    text = " ".join(str(value or "").split()).strip()
    if text:
        xmp[key] = text


def _xmp_bag(xmp, key: str, values) -> None:
    import exiv2

    if isinstance(values, str):
        values = [values]
    items = [" ".join(str(item).split()).strip()
             for item in (values or []) if str(item or "").strip()]
    if not items:
        return
    array = exiv2.XmpArrayValue(exiv2.TypeId.xmpBag)
    for item in items:
        array.read(item)
    xmp[key] = array


def _write_catalog_fields(xmp, fields: dict, *, rights_only: bool) -> None:
    import exiv2

    _xmp_text(xmp, "Xmp.dc.rights", fields.get("copyright"))
    _xmp_text(xmp, "Xmp.dc.creator", fields.get("creator"))
    if rights_only:
        return
    try:
        exiv2.XmpProperties.registerNs(LIGHTROOM_NAMESPACE, "lr")
    except Exception:  # noqa: BLE001 - already registered on a second export
        pass
    _xmp_text(xmp, "Xmp.dc.title", fields.get("title"))
    _xmp_text(xmp, "Xmp.dc.description", fields.get("caption"))
    _xmp_bag(xmp, "Xmp.dc.subject", fields.get("keywords"))
    _xmp_bag(xmp, "Xmp.lr.hierarchicalSubject", fields.get("keywordPaths"))
    _xmp_text(xmp, "Xmp.xmp.Label", fields.get("label"))
    try:
        rating = int(fields["rating"])
    except (KeyError, TypeError, ValueError):
        return
    xmp["Xmp.xmp.Rating"] = str(max(0, min(5, rating)))


def write_metadata(dst: Path | str, source: Path | str | None = None,
                   policy: str = "all-except-location",
                   fields: dict | None = None,
                   warnings: list[str] | None = None) -> bool:
    """Embed EXIF and XMP into an already-written export, honouring ``policy``.

    ``none`` writes nothing. ``copyright`` writes only ``dc:rights`` and
    ``dc:creator``. ``all`` copies the source EXIF, and ``all-except-location``
    is ``all`` with every ``Exif.GPSInfo.*`` key dropped. Every policy but
    ``none`` also stamps orientation 1 (export pixels are already oriented),
    the real pixel dimensions, and ``Software``. The ICC profile written by
    ``color_pipeline.save_export_image()`` survives untouched because exiv2
    edits the existing container rather than re-encoding it. Returns True when
    it wrote; a metadata failure logs and returns False so the pixels survive.
    """
    policy = str(policy or "").lower()
    if policy not in METADATA_POLICIES:
        policy = "all-except-location"
    if policy == "none":
        return False
    destination = Path(dst)
    fields = fields if isinstance(fields, dict) else {}
    reader = None
    staged = None
    try:
        import exiv2

        staged = durable_io.temporary_path(destination, "metadata")
        shutil.copyfile(destination, staged)
        image = exiv2.ImageFactory.open(str(staged))
        image.readMetadata()
        exif = image.exifData()
        if policy in ("all", "all-except-location"):
            origin = Path(source) if source else None
            if origin is not None and origin.is_file():
                # Keep the reader alive until the write lands.
                reader = exiv2.ImageFactory.open(str(origin))
                reader.readMetadata()
                _copy_source_exif(exif, reader.exifData(),
                                  keep_location=policy == "all", warnings=warnings)
            elif source and warnings is not None:
                warnings.append("Source camera metadata could not be read; the original is unavailable.")
        if policy != "all":
            _erase_gps(exif)
        exif["Exif.Image.Orientation"] = 1
        exif["Exif.Image.Software"] = SOFTWARE_TAG
        width, height = int(image.pixelWidth()), int(image.pixelHeight())
        if width > 0 and height > 0:
            exif["Exif.Photo.PixelXDimension"] = width
            exif["Exif.Photo.PixelYDimension"] = height
        xmp = image.xmpData()
        _write_catalog_fields(xmp, fields, rights_only=policy == "copyright")
        if fields.get("captureTime") and policy in ("all", "all-except-location"):
            import capture_time
            corrected = capture_time.exif_fields(fields["captureTime"])
            exif["Exif.Photo.DateTimeOriginal"] = corrected["DateTimeOriginal"]
            # Digitization may have happened later (for example, a scanned print).
            # A capture-clock correction must preserve that separate timestamp.
            if corrected["OffsetTimeOriginal"]:
                exif["Exif.Photo.OffsetTimeOriginal"] = corrected["OffsetTimeOriginal"]
            else:
                position = exif.findKey(exiv2.ExifKey("Exif.Photo.OffsetTimeOriginal"))
                if position != exif.end():
                    exif.erase(position)
            _xmp_text(xmp, "Xmp.exif.DateTimeOriginal", fields["captureTime"])
            _xmp_text(xmp, "Xmp.photoshop.DateCreated", fields["captureTime"])
        image.setExifData(exif)
        image.setXmpData(xmp)
        image.writeMetadata()
        # A failing metadata writer must not damage the successfully encoded
        # pixels. Publish only after the complete container write succeeds.
        del image
        durable_io.publish_file(staged, destination)
        return True
    except Exception as error:  # noqa: BLE001 - pixels outrank metadata
        print(f"write_metadata: {destination.name}: {error}", file=sys.stderr)
        if warnings is not None:
            warnings.append(f"Requested metadata could not be saved: {error}")
        return False
    finally:
        del reader
        if staged is not None:
            staged.unlink(missing_ok=True)
