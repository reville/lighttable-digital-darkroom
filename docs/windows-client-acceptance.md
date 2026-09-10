# Windows client acceptance

Verified September 10, 2026. **Windows 0.6.1 passed native Windows 10/11 x64 acceptance and Windows 11 ARM x64-emulation acceptance.** The signed installer and portable ZIP are published on [v0.6.1](https://github.com/reville/lighttable-digital-darkroom/releases/tag/v0.6.1); the [installation guide](https://lighttable.app/windows.html) explains processor selection and signature/checksum verification.

## Exact release

- Application source and immutable `v0.6.1` tag: `2382de7ccb6a2e81c304a6c9116bf578b95aee69`.
- Signed build: [34506394820](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34506394820).
- Installer: `LightTable-0.6.1-windows-x64-setup.exe`, 439,153,216 bytes.
- Installer SHA-256: `d5a4a0e3ba069b93cb9f0157814a50a5eece32478b3aa69ed1a935e7b07bb951`.
- Portable ZIP: 292,263,647 bytes; SHA-256 `4916b83ed786752d84666c5c4c44cebdab23a43c75665e775eccb44eedef2356`.
- Embedded source, version, Windows/x64 identity and `source_dirty: false` were independently verified. The original build's artifact sizes/hashes and all 420 packaged native-file hashes match the downloaded bytes. Windows reports 421 valid installed PE signatures, including the uninstaller. The certificate identifies Nicholas Reville; installer metadata is Chonkers LLC.

The package source is separate from the tested acceptance/promotion tooling revision `b4bd228708f663c8499534122ea025afc8baaf52` ([PR #131](https://github.com/reville/lighttable-digital-darkroom/pull/131)). Native tests select the original build and require its exact installer hash; no rebuild is substituted.

## Passed client matrix

| Target | Runtime case | Offline, unelevated install | Native edit, RGB16 export and reopen |
| --- | --- | --- | --- |
| Windows 10 x64, build 19045 | WebView2 genuinely absent | Passed | Passed |
| Windows 11 x64, build 26200 | Normal preinstalled WebView2 preserved | Passed | Passed |
| Windows 11 ARM64, build 26200 | Hosted runtime; x64 application under emulation | Install/repair/removal passed; offline absence not claimed | Passed |

[Windows 10/11 run 34510352623](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34510352623) passed both jobs in disposable native AMD64 client VMs. Both guest receipts confirm networking disabled, installation without elevation, the exact installer digest, valid installed signatures, and successful native rendering/edit/export/restart. Windows 10 reports the `absent` case and actual runtime absence; Windows 11 reports `preinstalled`, initial presence, and no fabricated absence. Microsoft [documents WebView2 as preinstalled on Windows 11](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution).

[ARM run 34510354937](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34510354937) passed on Windows 11 Enterprise ARM64, image `20260906.161.1`. The same signed installer passed installation, same-version repair, CLI registration and data-preserving removal. All 421 installed PE signatures were valid. The extracted x64 app passed rendering, saved exposure/rating, server restart and RGB16 TIFF export under Windows x64 emulation.

All downloaded native TIFF exports independently match SHA-256 `dfadcf6a657eadb64bd7060652b3d4af2f39e62bc0a43104ded75d1523b333cf`. The reports describe a 128 × 1024 RGB uint16 export with an embedded ICC profile.

## Publication and updates

[Preparation 34510377102](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34510377102) retained the original binaries and generated versioned checksums and installer recipes. [Promotion 34513680232](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34513680232) independently verifies the public download bytes and the appcast Ed25519 signature against the key embedded in the actual executable before advancing the Windows feed. The [canonical release manifest](../release/manifest.json) records the published platform and receipts. macOS and Linux keep their own versions and artifacts.

Direct installer installations use the signed Windows feed on the `desktop-updates` release; portable ZIP installations update manually. This is the first public Windows binary release, so an old-to-new public Windows updater journey is not claimed. Prepared Scoop, WinGet and Chocolatey recipes are not published package-manager listings. Microsoft Store certification is separate.

## Coverage limits and next hardware checks

Windows support remains experimental. These VMs use a virtual display adapter; they do not establish physical GPU, display scaling, color management, performance or large-RAW behavior. Certificate trust was checked online before disconnecting, so this is not cold-cache certificate verification. ARM testing does not establish a native ARM64 package or an ARM missing-runtime offline case.

The next useful checks are physical Intel/AMD Windows PCs and a Snapdragon Windows 11 device, including real GPU drivers, color/display behavior, representative large RAWs and update/recovery. The existing disposable VM workflow remains available for repeatable installer regression tests. Alternative appropriately licensed client VMs can provide additional OS-image coverage; Windows Server results cannot replace Windows client evidence.

## Superseded candidates

The 0.6.0 staging draft remains historical. Its [Windows 11 ARM run 34436651354](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34436651354) passed, and [client run 34481336232](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34481336232) passed Windows 10. The old Windows 11 job failed a runtime-absence gate despite successful runtime-present installation/native checks; it is not relabeled as passing. The old ZIP also lacks the newer required clean-source/platform metadata. Fresh 0.6.1 bytes and receipts supersede those publication blockers without modifying the old artifacts.

Draft [PR #114](https://github.com/reville/lighttable-digital-darkroom/pull/114) is closed as superseded; its ARM workflow and updated VM harness were already integrated through [PR #122](https://github.com/reville/lighttable-digital-darkroom/pull/122). Use the current manual workflows and [release process](../release/process.md) for future candidates, not the old pinned 0.5.1 harness or expired monitor.
