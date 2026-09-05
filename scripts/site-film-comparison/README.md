# Website film comparison

This pipeline rebuilds the evidence block at the bottom of the LightTable
website from one real RAW file. It produces a neutral conversion, an imported
Lightroom-compatible XMP conversion with Film off, a LightTable physical film
render, matched 100% crops, and two print/scan recipes.

From the app directory:

```sh
MPLCONFIGDIR=/private/tmp/lighttable-mpl \
  .venv/bin/python scripts/build-site-film-comparison.py \
  --output /path/to/lighttable-site/assets/film-comparison
.venv/bin/python scripts/build-site-film-comparison.py --check \
  --output /path/to/lighttable-site/assets/film-comparison
```

You can set `LIGHTTABLE_SITE_ROOT=/path/to/lighttable-site` instead of passing
`--output`. The app repository intentionally does not carry a duplicate website;
the generated files belong in the public `reville/lighttable-site` repository.

The default source is the CC0 Nikon Z6 still life. Fetch it with
`python3 scripts/fetch-demo-raws.py --file 06-nikon-z6-still-life-toys.NEF`. To use another RAW,
pass `--raw` plus its source and license fields. Use `--crop x,y,size` to lock a
different pixel-for-pixel crop.

The default middle column is **not** presented as an Adobe Lightroom render.
It is the result of converting `lighttable-natural.xmp` through LightTable's
real XMP importer, then applying the mapped controls in LightTable. If an
actual Lightroom comparison is needed, export an uncropped JPEG or TIFF from
Lightroom and pass it explicitly:

```sh
.venv/bin/python scripts/build-site-film-comparison.py \
  --adobe-reference /path/to/lightroom-export.tif \
  --output /path/to/lighttable-site/assets/film-comparison
```

The generated `assets/film-comparison/manifest.json` in the site checkout records that
provenance boundary, source and preset hashes, mapped and ignored XMP
operations, crop coordinates, complete film parameters, renderer profile
digest, and every output hash.
