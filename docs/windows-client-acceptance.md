# Windows client acceptance

Verified September 10, 2026. The Windows 11 ARM **x64-emulation test work is complete** for the signed 0.6.0 candidate. The ARM workflow, installer checks, native editing checks and updated x64 VM harness are already integrated through [PR #122](https://github.com/reville/lighttable-digital-darkroom/pull/122). Draft [PR #114](https://github.com/reville/lighttable-digital-darkroom/pull/114) is superseded; its old failed 0.5.1 VM run is not the current acceptance result.

## Exact candidate

- Application source: `0ae5e9aaf90b3554ad1d027d5b6b10667cd61ae2`.
- Signed Windows build: [34434174967](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34434174967).
- Installer: `LightTable-0.6.0-windows-x64-setup.exe`.
- Installer SHA-256: `e49f246e848069598849e4919cbf2be28015115524e33dbb156a03e7bbfc4aea`.
- Portable ZIP SHA-256: `a4cbba3bfe8ffc367acb5bf9da334462b504b32fe1f47b2f427dcd59f14bbf5c`.

No binary was rebuilt or published for this closeout. This is an x64 application; no native Windows ARM64 package exists.

## Windows 11 ARM: passed

[Run 34436651354](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34436651354) passed on Windows 11 Enterprise ARM64 build 26200, hosted image `20260906.161.1`, using tester revision `fe9bebbd343d39d607c898d876c28cce17207961`.

The signed installer passed installation, same-version repair, CLI registration and data-preserving removal. All 421 installed PE signatures were valid. The extracted x64 application passed native rendering, saved exposure/rating, server restart and RGB16 TIFF export under Windows x64 emulation. Its downloaded TIFF independently matched SHA-256 `dfadcf6a657eadb64bd7060652b3d4af2f39e62bc0a43104ded75d1523b333cf`.

The hosted image does not prove a native ARM64 build, offline installation with WebView2 absent, physical Snapdragon GPU/display behavior, or an old-to-new updater journey. The workflow explicitly records those boundaries. Use `.github/workflows/windows-native.yml` with `runner=windows-11-arm` for future candidates, keeping tester revision separate from package source and binding the installer digest to the original build.

## Windows 10/11 x64: newer results

[Run 34481336232](https://github.com/reville/lighttable-digital-darkroom/actions/runs/34481336232) tested that same installer in disposable native AMD64 client VMs with networking disabled and installation running without elevation.

| Check | Windows 10 build 19045 | Windows 11 build 26200 |
| --- | --- | --- |
| Offline install, repair, CLI and removal | Passed | Passed |
| Installed executable signatures | 421 valid | 421 valid |
| Native editing, TIFF export and reopen | Passed | Passed |
| WebView2 absent before offline install | Passed | Not established; runtime preinstalled |
| Overall job | Passed | Failed the runtime-absence gate |

The Windows 11 host and installer receipts report success; the overall job correctly failed the additional prerequisite-absence condition. Microsoft [documents WebView2 as preinstalled on Windows 11](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution). The vendor uninstaller did not establish absence on this image. No detection keys were removed to imitate it. Certificate trust was checked online before disconnecting; this is not cold-cache certificate verification. The VMs use a virtual display adapter, so they do not establish physical GPU or color/display quality.

## Remaining release gates and test choices

Windows remains blocked in the [canonical release manifest](../release/manifest.json). Two issues remain:

1. The currently declared gate requires Windows 11 offline installation with WebView2 genuinely absent. Keep that case open unless a deliberate release-policy decision accepts Windows 10 absence coverage plus normal Windows 11 runtime-present coverage. Do not relabel the failed run as passing.
2. The older 0.6.0 ZIP lacks the clean-source and platform fields required by the newer preparation verifier. It cannot pass automated promotion as-is. A future versioned candidate should include the required embedded metadata and repeat acceptance; never change existing versioned binary bytes or invent a clean-source receipt.

For further testing:

- **Existing disposable VMs:** retain the successful Windows 10 case, report Windows 11 runtime-present compatibility separately, and find a supported image/setup where prerequisite absence is real if that case remains mandatory. No new cloud account is needed for this route.
- **Physical Windows PCs:** test an Intel/AMD Windows 10 and Windows 11 machine, including actual GPU drivers, display scaling, color management, large RAWs and update/recovery behavior. This fills hardware gaps that the VMs cannot cover. Windows 11 may still include WebView2, so hardware alone does not solve the absence case.
- **Azure client VMs:** an alternative repeatable environment if appropriately licensed client images and an Azure subscription are available. [Microsoft's dev/test requirements](https://learn.microsoft.com/en-us/azure/virtual-machines/windows/client-images) apply. A Windows Server VM cannot substitute for Windows 10/11 client evidence, and a different provider does not by itself remove the preinstalled runtime.

The current manual workflow is `.github/workflows/windows-client-vm.yml`, with explicit candidate version/source/build/hash inputs, bounded jobs, real guest receipts and automatic VM cleanup. Do not restart the superseded pinned 0.5.1 workflow or its expired monitor. See [release/process.md](../release/process.md) for preparation and immutable publication.
