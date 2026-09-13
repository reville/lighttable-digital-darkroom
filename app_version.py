# SPDX-License-Identifier: GPL-3.0-only
"""The one LightTable release version.

About, the local API, the command client and every build without an explicit
version read this. Bump it before tagging a release with
`python3 scripts/release/set-version.py X.Y.Z`; the release workflow refuses a
tag that disagrees with it.
"""

VERSION = "0.7.0"


if __name__ == "__main__":
    print(VERSION)
