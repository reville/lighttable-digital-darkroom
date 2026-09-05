# CC0 and public-domain RAW demo collection

Thirty-five camera-original RAW photos selected for LightTable screenshots, demo
videos, compatibility checks, and visual regression work. Files 21–35 all
contain people; that subset includes portraits, crowds, street scenes, and five
night/evening photographs (25–29). Files 31–35 are well-lit portraits selected
specifically for skin-tone work, including two tight head-and-shoulders frames,
a formal portrait, and an environmental portrait. The full set covers every RAW
extension currently accepted by the app: `.ARW`, `.CR2`, `.CR3`, `.DNG`, `.NEF`,
`.ORF`, `.RAF`, and `.RW2`.

## Rights and provenance

Files 01–20 were individually marked **Creative Commons Zero (CC0) 1.0 —
Public Domain** by [raw.pixls.us](https://raw.pixls.us/). Files 21–25 carry
explicit CC0 declarations by their photographers on the linked PIXLS.US posts.
Files 26–30 come from the photographer's [public-domain highlight-recovery test
set](https://discuss.pixls.us/t/highlight-recovery-test-set/28801); each RAW also
contains `Copyright=Public Domain` metadata. File 31 is a camera-original
Wesaturate image mirrored by [One Raw](https://www.oneraw.co.uk/01/), which
identifies the image as courtesy of Wesaturate; Wesaturate's founders stated
that its RAW and JPEG photos were released under CC0 in the site's
[contemporaneous launch announcement](https://petapixel.com/2017/03/24/wesaturate-lets-download-free-raw-photos-editing-practice/).
Files 32–35 are camera-original Nikon NEFs preserved by the former Wikimedia
Commons Archive. Their archived RAW description pages explicitly dedicated each
file under CC0, and the linked Wikimedia Commons display images remain marked
CC0. The originals were recovered from the Internet Archive's
[2022 Commons Archive image snapshot](https://archive.org/details/wiki-commonsarchive).
The manifest distinguishes the exact declaration used for every file.

CC0 permits copying, modifying, distributing, and commercial use without
permission; see the [CC0 1.0 deed](https://creativecommons.org/publicdomain/zero/1.0/)
and [legal code](https://creativecommons.org/publicdomain/zero/1.0/legalcode.en).

`manifest.tsv` records source identifiers and provenance URLs, the applicable
rights declaration, source date, camera details, byte size, and SHA-256 digest
for each file. Keep that manifest with redistributed copies even when
attribution is not required. Do not infer that unrelated raw.pixls.us or
PIXLS.US files have the same rights; both sites contain files under more
restrictive licenses.

The people subset has no accompanying model-release documentation. CC0 or a
public-domain dedication addresses the photographer's copyright, but does not
waive a subject's privacy or publicity rights. For public-facing promotional
use, review the intended context and avoid implying endorsement by a depicted
person, photographer, venue, event, camera maker, property owner, or source.

## Use

Open `files/` as the photo folder in LightTable, or launch the local app with:

```sh
bash run.sh "$PWD/demo-assets/cc0-raw/files"
```

The numeric filenames keep the contact-sheet order stable. The descriptive
subject names are local catalog labels; the original source filenames are in
`manifest.tsv`.

Run `./demo-assets/cc0-raw/verify.sh` from the repository root to verify all 35
files against `SHA256SUMS` and check any existing macOS build products for
accidental inclusion.

## Repository and build boundary

The large RAW originals are not committed to this public repository. The source
manifest and checksums are retained; small, unmodified embedded JPEG previews
with provenance live in `tests/fixtures/photos` for ordinary CI and release tests.

Fetch the eight-format RAW compatibility set when needed:

```sh
python3 scripts/fetch-demo-raws.py
```

Use `--all-direct` for all 20 samples with recorded direct URLs, or
`--file 06-nikon-z6-still-life-toys.NEF` for one. Downloads are verified against
the original size and SHA-256. The other samples require retrieval from their
recorded source pages; the tool does not guess a download URL. Downloaded RAWs
are ignored by Git.

This directory is repository-only. The macOS development build, self-contained
macOS release build, and Windows release build all assemble their payloads from
explicit allowlists; none copies `demo-assets/`. Do not add this directory to a
packaging allowlist.
