# Windows client acceptance

Verified September 18, 2026 for LightTable 0.7.10. Windows support remains experimental; this document records the exact release evidence and its limits.

## Exact release and artifacts

- Source: `75a719e0b4bec18371ea62f879440316dbab7560`.
- Signed build: [35390515279](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35390515279), passed.
- Installer: 524,440,176 bytes; SHA-256 `b42634fb567c2c4acaeae69d66d708d8eea0965d73417d2228d3e4be5c10f2b1`.
- Portable ZIP: 394,397,046 bytes; SHA-256 `c53efd346b6daaa5b660458a6364e49b650139b30002a5788af6942022f67567`.
- Build and Windows 10/11 installed signature receipts each report 451 PE files and zero invalid signatures.

The Windows startup fix from [PR #181](https://github.com/reville/lighttable-digital-darkroom/pull/181), commit `cf4f877d`, retries Windows sharing conflicts and leaves the server running when instance registration remains contended. Unit evidence: 2,443 tests, 23 skipped, 7 regressions passed in [run 34873146289](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34873146289). Startup font scan, shutdown race, and request queue improvements from [PR #195](https://github.com/reville/lighttable-digital-darkroom/pull/195), commit `1d2f6d88`, opt-in crash reporting from [PR #193](https://github.com/reville/lighttable-digital-darkroom/pull/193), commit `b72a35d2`, and complete 20-locale translation validation from [PR #197](https://github.com/reville/lighttable-digital-darkroom/pull/197) and [PR #201](https://github.com/reville/lighttable-digital-darkroom/pull/201) are included.

## Client matrix

| Target | Runtime case | Result |
| --- | --- | --- |
| Windows 10 x64, build 19045 | Exact offline, unelevated, WebView2 absent | Passed: native restart, saved exposure/rating, RGB16 export, HTTP 200 |
| Windows 11 x64, build 26200 | Preinstalled WebView2 preserved; offline, unelevated installation | Passed: native restart, saved exposure/rating, RGB16 export, HTTP 200 |
| Windows 11 ARM64, build 26200 | x64 app under emulation; hosted runtime | Historical 0.7.8 coverage in run 34980695512; not rerun for 0.7.10 |

Windows 10 and 11 evidence is in [client run 35395072859](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35395072859).

## Publication and updates

[Preparation run 35395135565](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35395135565) passed. Windows 10 and 11 x64 acceptance passed. [Promotion 35400944072](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35400944072) independently verified public download bytes and the production appcast signature, then advanced the Windows feed. The [signed installer and portable ZIP are public on v0.7.10](https://github.com/reville/lighttable-digital-darkroom/releases/tag/v0.7.10). Direct installations use WinSparkle; portable ZIP installations update manually. Package-manager publication is verified separately from this direct-release acceptance.

## Coverage limits

The VMs use virtual display adapters, so they do not establish physical GPU, scaling, color management, performance, or large-RAW behavior. Certificate trust was checked online before the offline cases. ARM testing does not establish a native ARM64 package or an offline missing-runtime case.
