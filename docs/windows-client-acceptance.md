# Windows client acceptance

The signed Windows 0.5.1 candidate can be tested without rebuilding or changing its release bytes. Keep the package source commit separate from the tester commit.

## Windows 11 ARM

Dispatch `windows-native.yml` with `runner=windows-11-arm`, the completed Windows build run ID, and the independently verified installer SHA-256. The workflow records the real OS/CPU, verifies the installer, exercises install/reinstall/CLI/data-preserving uninstall, and runs the extracted portable app's editing, restart and 16-bit TIFF export journey.

This tests the x64 application under Windows 11 ARM emulation. It does not create or prove a native ARM64 package. The hosted image has administrator privileges and preinstalled software; it does not establish fresh offline prerequisite installation or physical GPU/display behavior.

## Windows 10 and 11 x64

`windows-client-vm.yml` boots two fresh Microsoft Enterprise evaluation images on disposable GitHub Linux runners with KVM. This initial workflow intentionally pins the verified 0.5.1 build, installer digest, OS media URLs/checksums, and VM container digest. Review and change all candidate pins together for a future release.

The VM setup uses a minimal custom unattended configuration and does not run the container's default Windows customization script. Windows UAC, firewall and Defender defaults remain in place. No VM ports are published, no GitHub credentials enter the guest, and only public application/test files are staged. An ephemeral local account exists only within the disposable guest; its generated configuration and VM disks are never uploaded.

The elevated setup phase records the real OS/build, attempts WebView2 removal using Microsoft's own uninstaller, and checks the signed installer before disconnecting. This online certificate-chain check is recorded; it does not claim a completely cold certificate cache. It then disables all active network adapters and launches acceptance with the logged-on account's limited token. The test refuses to run elevated or with an active adapter.

`installer-smoke.ps1 -NativeAcceptanceReport ...` checks the actual installed GUI through `desktop-smoke.py`, then verifies repair, CLI registration and data-preserving uninstall. The new `--allow-disposable-direct-install` option is only for a disposable test account: a direct install can alter that account's WinSparkle preferences. The default desktop smoke still rejects direct installs, and package-manager-owned installs remain rejected even with the option.

The setup phase restores networking only after the tests finish, returns bounded evidence to a receiver inside the VM container, and shuts down Windows. Jobs have a 60-minute cap and always stop/remove the container. There is no paid VM provisioning.

## Interpreting evidence

- `host.json`: actual OS/CPU, UAC, runtime absence, network disconnection, online signature check and setup failures.
- `result.json`: limited-token install/repair/CLI/uninstall result and exact installer digest.
- `native/report.json`: installed shell, rendering, edit persistence across restart, HTTP health and TIFF export results.
- `installed-signatures.json`: Authenticode evidence for the installed native payload, including its uninstaller.
- `vm.log`: VM setup diagnostics. Review raw artifacts before sharing them publicly.

A job cannot pass the missing-prerequisite gate unless WebView2 was genuinely absent before offline installation. If the vendor's uninstaller cannot remove a built-in runtime, report that boundary rather than deleting detection keys. A virtual software-rendered desktop is useful client compatibility evidence; it is not a physical GPU/color/HDR/performance benchmark. Same-version reinstall is not a signed old-to-new updater journey.
