"""Use the bundled parallel decoder without regressing X-Trans development."""
from contextlib import contextmanager
from enum import Enum
import importlib


@contextmanager
def open_raw(path):
    stock = importlib.import_module("rawpy")
    try:
        decoder = importlib.import_module("rawpy_openmp")
    except ImportError:
        decoder = stock
    raw = None
    try:
        raw = decoder.imread(str(path))
        if (decoder is not stock and raw.is_xtrans
                and getattr(decoder, "LIGHTTABLE_XTRANS_WAVEFRONT", 0) != 1):
            # is_xtrans reads only the already-open header; it never unpacks
            # sensor pixels. Bayer takes one header open; X-Trans adds one
            # cheap header read for old wheels lacking the verified schedule.
            raw.close()
            raw = None
            decoder = stock
            raw = stock.imread(str(path))
        with raw:
            yield raw, decoder
    except Exception as error:
        if decoder is not stock and isinstance(error, decoder.LibRawError):
            error_type = getattr(stock, type(error).__name__, stock.LibRawError)
            raise error_type(str(error)) from error
        raise


def native_options(options, decoder):
    """The two extension modules have distinct enum classes with equal values."""
    return {key: getattr(decoder, type(value).__name__)(value.value)
            if isinstance(value, Enum) else value
            for key, value in options.items()}
