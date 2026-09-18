# Windows client acceptance

Verified September 18, 2026 for LightTable 0.7.9. Windows support remains experimental; this document records the exact release evidence and its limits.

## Exact release and artifacts

- Source: `72154a20b60e6be4f0175f328a3bed6af1390e2a`.
- Signed build: [35283559261](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35283559261), passed.
- Installer: 524,413,552 bytes; SHA-256 `cc644b34dd6a3992fa71887923e8b8a733f6232165980676b4318bb4669f6b6d`.
- Portable ZIP: 394,385,607 bytes; SHA-256 `0ce97c2c6c91bb9c45c13867b6c0ce598c5b30501156cdfe024822d94775eed1`.
- Build and Windows 10/11 installed signature receipts each report 451 PE files and zero invalid signatures.

The Windows startup fix from [PR #181](https://github.com/reville/lighttable-digital-darkroom/pull/181), commit `cf4f877d`, retries Windows sharing conflicts and leaves the server running when instance registration remains contended. Unit evidence: 2,443 tests, 23 skipped, 7 regressions passed in [run 34873146289](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34873146289). Startup font scan, shutdown race, and request queue improvements from [PR #195](https://github.com/reville/lighttable-digital-darkroom/pull/195), commit `1d2f6d88`, and opt-in crash reporting from [PR #193](https://github.com/reville/lighttable-digital-darkroom/pull/193), commit `b72a35d2`, are included.

## Client matrix

| Target | Runtime case | Result |
| --- | --- | --- |
| Windows 10 x64, build 19045 | Exact offline, unelevated, WebView2 absent | Passed: native restart, saved exposure/rating, RGB16 export, HTTP 200 |
| Windows 11 x64, build 26200 | Preinstalled WebView2 preserved; offline, unelevated installation | Passed: native restart, saved exposure/rating, RGB16 export, HTTP 200 |
| Windows 11 ARM64, build 26200 | x64 app under emulation; hosted runtime | Historical 0.7.8 coverage in run 34980695512; not rerun for 0.7.9 |

Windows 10 and 11 evidence is in [client run 35351530996](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35351530996).

## Publication and updates

[Preparation run 35351551772](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35351551772) passed. Windows 10 and 11 x64 acceptance passed. [Promotion 35360264901](https://github.com/reville/lighttable-digital-darkroom/actions/runs/35360264901) independently verified public download bytes and the production appcast signature, then advanced the Windows feed. The [signed installer and portable ZIP are public on v0.7.9](https://github.com/reville/lighttable-digital-darkroom/releases/tag/v0.7.9). Direct installations use WinSparkle; portable ZIP installations update manually. Package-manager publication is verified separately from this direct-release acceptance.

## Coverage limits

The VMs use virtual display adapters, so they do not establish physical GPU, scaling, color management, performance, or large-RAW behavior. Certificate trust was checked online before the offline cases. ARM testing does not establish a native ARM64 package or an offline missing-runtime case.
