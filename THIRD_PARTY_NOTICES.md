# Third-party components

- Optional face matching uses OpenCV Zoo SFace INT8 (Apache-2.0) and YuNet (MIT), pinned to `47534e27c9851bb1128ccc0102f1145e27f23f98`. Model downloads are verified against SHA-256 hashes in `film_lab_ai/face_models.py`. Their license texts are bundled in `film_lab_ai/licenses/`. The `opencv-python-headless` runtime retains its package license and third-party notices.

LightTable is distributed under GPL-3.0-only. Existing third-party copyright,
license, and attribution notices remain in their source files and license texts.

- The Python film pipeline is based on [agx-emulsion / spektrafilm](https://github.com/andreavolpato/agx-emulsion), pinned to `3bb2c2d2801ff68b92019cf1dbcbb133d60832bc`, GPLv3.
- The standalone Rust film engine is based on [spektrafilm-rs](https://github.com/turbasvin/spektrafilm-rs), pinned to `9dd59b0380194b93686aaa230a8bb9680aa270a4`, GPLv3. The adapted resident-engine source and license are included under `rust-engine/vendor/spektrafilm`.
- [Sparkle](https://github.com/sparkle-project/Sparkle), version 2.9.6, supplies macOS updates. Its framework retains its license notices.
- [WinSparkle](https://github.com/vslavik/winsparkle), version 0.9.4, supplies Windows updates. Windows bundles include its license and third-party notices.
- SCUNet network code is Apache-2.0 and the selected Kai Zhang denoising weights carry the recorded MIT license. `scripts/fetch-models.py` records the sources and hashes; release bundles include the model license texts and conversion provenance.
- Python dependencies and native components retain their individual distribution metadata. Their pinned versions are recorded in `requirements-runtime.lock`, `packaging/runtime-windows.lock`, `packaging/runtime-linux.lock`, and Cargo lockfiles. The Linux updater uses `cryptography` for Ed25519 verification.
- Windows packages include Microsoft Visual C++ x64 runtime DLLs from the Visual Studio redistributable directory. These Microsoft components retain their signatures and are covered by [Microsoft's Visual Studio license terms and distributable-code list](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files). The build manifest records the runtime version; updates ship with LightTable releases.
- macOS builds compile rawpy 0.26.1 and LibRaw 0.22.0 with OpenMP. `scripts/build-rawpy-openmp.sh` pins source commits; `scripts/build-rawpy-native.sh` pins source archives for LLVM OpenMP 19.1.7, libjpeg-turbo 3.2.0, Jasper 4.2.8, and Little CMS 2.19.1, compiled for macOS 13. Their license texts are installed in `licenses/rawpy-openmp`. The GPL demosaic packs remain disabled. `packaging/libraw-xtrans-determinism.patch` replaces the in-place X-Trans tile loop's unsafe parallelism with dependency-preserving diagonal scheduling; other LibRaw stages retain OpenMP.
- Small real-photo test fixtures retain their individual CC0 source records in `tests/fixtures/photos/provenance.json`. They are excluded from installed app payloads.

Build scripts fetch pinned upstream sources rather than copying development
checkouts. The corresponding source revisions and this repository's build
instructions are part of the release record. Camera/profile data and fetched
color profiles retain their upstream notices.
