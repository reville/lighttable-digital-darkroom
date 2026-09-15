# Windows client acceptance

Verified September 15, 2026 for LightTable 0.7.8. Windows support remains experimental; this document records the exact release evidence and its limits.

## Exact release and artifacts

- Source: `a6482d0ba3c43921bfa09cb8290f44d600daca37`.
- Signed build: [34974846024](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34974846024), passed.
- Installer: 524,363,904 bytes; SHA-256 `b09539c0111d196a8dfb3714151b04fd8e579f5508d8e2a77c758f625baae241`.
- Portable ZIP: 394,303,097 bytes; SHA-256 `ea4696f96bc202fe291fc571a7b6076961b5e72d5b0f296b7c0346933be1223e`.
- Build and Windows 10/11/ARM installed signature receipts each report 451 PE files and zero invalid signatures.

The Windows startup fix from [PR #181](https://github.com/reville/lighttable-digital-darkroom/pull/181), commit `cf4f877d`, retries Windows sharing conflicts and leaves the server running when instance registration remains contended. Unit evidence: 2,443 tests, 23 skipped, 7 regressions passed in [run 34873146289](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34873146289).

## Client matrix

| Target | Runtime case | Result |
| --- | --- | --- |
| Windows 10 x64, build 19045 | Exact offline, unelevated, WebView2 absent | Passed: native restart, saved exposure/rating, RGB16 export, HTTP 200 |
| Windows 11 x64, build 26200 | Preinstalled WebView2 preserved; offline, unelevated installation | Passed: native restart, saved exposure/rating, RGB16 export, HTTP 200 |
| Windows 11 ARM64, build 26200 | x64 app under emulation; hosted runtime | Passed installer, repair/removal, native x64 edit/export/restart |

Windows 10 and 11 evidence is in [client run 34980692480](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34980692480). ARM evidence is in [run 34980695512](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34980695512). The Windows build, Windows 10, Windows 11, and ARM TIFF exports match SHA-256 `83cc52c7e8da5bacf26614df701dd1277a4a0d51c0e74bbf1ada3bdab1b8a4e3`.

## Publication and updates

[Preparation run 34982820806](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34982820806) passed. Windows 10 and 11 x64 acceptance passed. [Promotion 34984671390](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34984671390) independently verified public download bytes and the production appcast signature, then advanced the Windows feed. The [signed installer and portable ZIP are public on v0.7.8](https://github.com/reville/lighttable-digital-darkroom/releases/tag/v0.7.8). Direct installations use WinSparkle; portable ZIP installations update manually. Package-manager publication is verified separately from this direct-release acceptance.

## Coverage limits

The VMs use virtual display adapters, so they do not establish physical GPU, scaling, color management, performance, or large-RAW behavior. Certificate trust was checked online before the offline cases. ARM testing does not establish a native ARM64 package or an offline missing-runtime case. Historical 0.7.7 failures were not published and do not count as 0.7.8 evidence.
