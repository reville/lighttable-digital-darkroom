// SPDX-License-Identifier: GPL-3.0-only
#[cfg(target_os = "windows")]
fn main() {
    println!("cargo:rerun-if-env-changed=LIGHTTABLE_ICON_ICO");
    println!("cargo:rerun-if-changed=app.manifest");
    let mut resources = winresource::WindowsResource::new();
    // rfd's common-controls-v6 feature imports TaskDialogIndirect at load time.
    // This dependency is required even for builds without a custom icon.
    resources.set_manifest(include_str!("app.manifest"));
    if let Ok(icon) = std::env::var("LIGHTTABLE_ICON_ICO") {
        resources.set_icon(&icon);
    }
    resources
        .compile()
        .expect("could not compile the Windows application resources");
}

#[cfg(not(target_os = "windows"))]
fn main() {}
