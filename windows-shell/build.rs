#[cfg(target_os = "windows")]
fn main() {
    println!("cargo:rerun-if-env-changed=LIGHTTABLE_ICON_ICO");
    if let Ok(icon) = std::env::var("LIGHTTABLE_ICON_ICO") {
        winresource::WindowsResource::new()
            .set_icon(&icon)
            .compile()
            .expect("could not compile the Windows application resources");
    }
}

#[cfg(not(target_os = "windows"))]
fn main() {}
