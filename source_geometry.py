"""Source pixel geometry from decoder headers, without decoding image pixels."""
from pathlib import Path

import media_formats


def metadata(path: Path) -> dict[str, int]:
    """Return unrotated output dimensions and the decoder's orientation.

    RAW EXIF can describe an embedded JPEG, a sensor border, or a full-size
    sensor even when the file contains a reduced-resolution RAW. LibRaw's
    output size is the geometry used by our actual rendering pipeline.
    """
    if path.suffix.lower() in media_formats.RAW_EXTENSIONS:
        import rawpy
        with rawpy.RawPy() as raw:
            raw.open_file(str(path))
            size = raw.sizes
            if size.pixel_aspect != 1.0:
                raise ValueError("Decoder adjusts non-square source pixels")
            width, height = size.iwidth, size.iheight
            orientation = {0: 1, 3: 3, 5: 8, 6: 6}.get(size.flip)
            if orientation is None:
                raise ValueError("Source orientation is unavailable")
    else:
        from PIL import Image
        with Image.open(path) as image:
            width, height = image.size
            orientation = int(image.getexif().get(274, 1))
    if width <= 0 or height <= 0:
        raise ValueError("Source dimensions are unavailable")
    return {"width": int(width), "height": int(height), "orientation": orientation}


def dimensions(path: Path) -> tuple[int, int]:
    """Return the displayed dimensions, including camera orientation."""
    info = metadata(path)
    width, height = info["width"], info["height"]
    return (height, width) if info["orientation"] in (5, 6, 7, 8) else (width, height)
