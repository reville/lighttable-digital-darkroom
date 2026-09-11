# Synthetic RAW fixtures

Written by `tests/make_raw_fixtures.py`, not captured by a camera.
They are valid uncompressed DNGs carrying a known colour matrix, a
known as-shot neutral, clipped highlights and a noisy flat field, so
the capture stage can be checked without shipping a camera original
or tracking its licence. Real-format coverage lives in the RAW
journey layers described in docs/journey-testing.md.

Rebuild with:

```sh
python tests/make_raw_fixtures.py
```
