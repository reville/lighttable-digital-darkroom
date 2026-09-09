//! Windows update ownership and the WinSparkle 0.9.4 adapter.
//!
//! Only an explicitly marked, Authenticode-signed direct installation can
//! install an EXE update. ZIPs and package-owned trees never enter WinSparkle.
use crate::localization::tr;
use serde_json::{Value, json};
use std::path::Path;

pub const FEED_URL: &str = "https://github.com/reville/lighttable-digital-darkroom/releases/download/desktop-updates/appcast-windows-x64.xml";
pub const PUBLIC_KEY: &str = "mrBmSL8f0FRN8j/imZxCWdCt0L4N3zP9kgOleH46eXA=";

pub fn status(directory: &Path) -> Value {
    let channel = std::fs::read_to_string(directory.join("install-channel.txt"))
        .unwrap_or_else(|_| "portable".into());
    channel_status(
        channel.trim_start_matches('\u{feff}').trim(),
        std::fs::read(directory.join("build-manifest.json"))
            .ok()
            .and_then(|bytes| {
                serde_json::from_slice::<Value>(
                    bytes.strip_prefix(&[239, 187, 191]).unwrap_or(&bytes),
                )
                .ok()
            })
            .is_some_and(|manifest| manifest["authenticode_signed"] == true),
    )
}

pub fn channel_status(channel: &str, signed: bool) -> Value {
    let (supported, managed, message) = match channel {
        "direct" if signed => (true, None, tr("LightTable checks for signed updates.")),
        "direct" => (
            false,
            None,
            tr("Automatic installation is unavailable in this unsigned build."),
        ),
        "scoop" => (false, Some("Scoop"), tr("Update LightTable with Scoop.")),
        "winget" => (false, Some("WinGet"), tr("Update LightTable with WinGet.")),
        "chocolatey" => (
            false,
            Some("Chocolatey"),
            tr("Update LightTable with Chocolatey."),
        ),
        _ => (
            false,
            Some("portable"),
            tr("Download the latest portable ZIP to update this copy of LightTable."),
        ),
    };
    json!({"type":"updateStatus", "supported":supported, "managedBy":managed, "message":message})
}

#[cfg(target_os = "windows")]
pub mod native {
    use super::*;
    use anyhow::{Context, Result, bail};
    use std::{
        ffi::{CString, OsString, c_char, c_void},
        fs,
        io::Write,
        os::windows::{
            ffi::{OsStrExt, OsStringExt},
            process::CommandExt,
        },
        path::PathBuf,
        process::{Command, Stdio},
        sync::{Mutex, OnceLock, mpsc},
        thread,
        time::{Duration, Instant},
    };

    // C ABI declarations intentionally use only the pinned WinSparkle header.
    // LoadLibraryExW restricts dependencies to the DLL directory and system paths.
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn LoadLibraryExW(name: *const u16, file: *mut c_void, flags: u32) -> *mut c_void;
        fn GetProcAddress(module: *mut c_void, name: *const c_char) -> *mut c_void;
        fn FreeLibrary(module: *mut c_void) -> i32;
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn WaitForSingleObject(handle: *mut c_void, timeout: u32) -> u32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    const TIMEOUT: Duration = Duration::from_secs(120);

    pub enum Request {
        Prepare(mpsc::Sender<bool>),
        RunInstaller(PathBuf, mpsc::Sender<bool>),
        InstallerStaged(Result<PathBuf, String>, mpsc::Sender<bool>),
        Shutdown,
        Cancel,
    }
    type Dispatch = Box<dyn Fn(Request) + Send>;
    static DISPATCH: OnceLock<Mutex<Dispatch>> = OnceLock::new();
    fn dispatch(request: Request) {
        if let Some(callback) = DISPATCH.get().and_then(|value| value.lock().ok()) {
            callback(request);
        }
    }
    fn ask(request: impl FnOnce(mpsc::Sender<bool>) -> Request) -> bool {
        let (tx, rx) = mpsc::channel();
        dispatch(request(tx));
        let ready = rx.recv_timeout(TIMEOUT).unwrap_or(false);
        if !ready {
            dispatch(Request::Cancel);
        }
        ready
    }
    unsafe extern "C" fn can_shutdown() -> i32 {
        i32::from(ask(Request::Prepare))
    }
    unsafe extern "C" fn shutdown() {
        dispatch(Request::Shutdown);
    }
    unsafe extern "C" fn cancel() {
        dispatch(Request::Cancel);
    }
    unsafe extern "C" fn run_installer(path: *const u16) -> i32 {
        if path.is_null() {
            return -1;
        }
        let mut length = 0;
        // WinSparkle supplies a NUL-terminated Windows path. Keep reads bounded.
        while length < 32768 && unsafe { *path.add(length) } != 0 {
            length += 1;
        }
        if length == 32768 {
            return -1;
        }
        let path = PathBuf::from(OsString::from_wide(unsafe {
            std::slice::from_raw_parts(path, length)
        }));
        if ask(|reply| Request::RunInstaller(path, reply)) {
            1
        } else {
            -1
        }
    }
    type VoidFn = unsafe extern "C" fn();
    type IntFn = unsafe extern "C" fn(i32);
    pub struct Updater {
        module: *mut c_void,
        cleanup: VoidFn,
        check: VoidFn,
        automatic: IntFn,
    }
    impl Updater {
        pub fn new(
            directory: &Path,
            enabled: bool,
            send: impl Fn(Request) + Send + 'static,
        ) -> Result<Self> {
            if status(directory)["supported"] != true {
                bail!("This installation is managed outside LightTable");
            }
            let bytes = fs::read(directory.join("build-manifest.json"))?;
            let manifest: Value =
                serde_json::from_slice(bytes.strip_prefix(&[239, 187, 191]).unwrap_or(&bytes))?;
            let version = manifest["version"]
                .as_str()
                .context("Update version is missing")?;
            let library: Vec<u16> = directory
                .join("WinSparkle.dll")
                .as_os_str()
                .encode_wide()
                .chain(Some(0))
                .collect();
            let module = unsafe {
                LoadLibraryExW(
                    library.as_ptr(),
                    std::ptr::null_mut(),
                    0x00000100 | 0x00001000,
                )
            };
            if module.is_null() {
                bail!("The signed update component could not be loaded");
            }
            macro_rules! symbol {
                ($name:literal, $type:ty) => {{
                    let name = CString::new($name)?;
                    let address = unsafe { GetProcAddress(module, name.as_ptr()) };
                    if address.is_null() {
                        unsafe {
                            FreeLibrary(module);
                        }
                        bail!(concat!("WinSparkle is missing ", $name));
                    }
                    unsafe { std::mem::transmute::<*mut c_void, $type>(address) }
                }};
            }
            let initialize = symbol!("win_sparkle_init", VoidFn);
            let cleanup = symbol!("win_sparkle_cleanup", VoidFn);
            let check = symbol!("win_sparkle_check_update_with_ui", VoidFn);
            let automatic = symbol!("win_sparkle_set_automatic_check_for_updates", IntFn);
            let interval = symbol!("win_sparkle_set_update_check_interval", IntFn);
            let appcast = symbol!(
                "win_sparkle_set_appcast_url",
                unsafe extern "C" fn(*const c_char)
            );
            let key = symbol!(
                "win_sparkle_set_eddsa_public_key",
                unsafe extern "C" fn(*const c_char) -> i32
            );
            let details = symbol!(
                "win_sparkle_set_app_details",
                unsafe extern "C" fn(*const u16, *const u16, *const u16)
            );
            let set_prepare = symbol!(
                "win_sparkle_set_can_shutdown_callback",
                unsafe extern "C" fn(unsafe extern "C" fn() -> i32)
            );
            let set_shutdown = symbol!(
                "win_sparkle_set_shutdown_request_callback",
                unsafe extern "C" fn(VoidFn)
            );
            let set_cancel = symbol!(
                "win_sparkle_set_update_cancelled_callback",
                unsafe extern "C" fn(VoidFn)
            );
            let set_error = symbol!(
                "win_sparkle_set_error_callback",
                unsafe extern "C" fn(VoidFn)
            );
            let set_installer = symbol!(
                "win_sparkle_set_user_run_installer_callback",
                unsafe extern "C" fn(unsafe extern "C" fn(*const u16) -> i32)
            );
            let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
            if DISPATCH.set(Mutex::new(Box::new(send))).is_err() {
                unsafe {
                    FreeLibrary(module);
                }
                bail!("The updater was already initialized");
            }
            unsafe {
                details(
                    wide("LightTable").as_ptr(),
                    wide("LightTable").as_ptr(),
                    wide(version).as_ptr(),
                );
                appcast(CString::new(FEED_URL)?.as_ptr());
                if key(CString::new(PUBLIC_KEY)?.as_ptr()) != 1 {
                    FreeLibrary(module);
                    bail!("The update signing key is invalid");
                }
                interval(86400);
                automatic(i32::from(enabled));
                set_prepare(can_shutdown);
                set_shutdown(shutdown);
                set_cancel(cancel);
                set_error(cancel);
                set_installer(run_installer);
                initialize();
            }
            Ok(Self {
                module,
                cleanup,
                check,
                automatic,
            })
        }
        pub fn check(&self) {
            unsafe {
                (self.check)();
            }
        }
        pub fn automatic(&self, enabled: bool) {
            unsafe {
                (self.automatic)(i32::from(enabled));
            }
        }
    }
    impl Drop for Updater {
        fn drop(&mut self) {
            unsafe {
                (self.cleanup)();
                FreeLibrary(self.module);
            }
        }
    }

    /// WinSparkle has verified Ed25519 before this callback. Copy both the
    /// installer and helper outside the installation so no live executable is
    /// replaced. The helper acknowledges process handles before GUI shutdown.
    pub fn stage_installer(
        installer: &Path,
        executable: &Path,
        server_pid: u32,
    ) -> Result<PathBuf> {
        if !installer.is_file()
            || installer
                .extension()
                .is_none_or(|ext| !ext.eq_ignore_ascii_case("exe"))
        {
            bail!("The update is not a Windows installer");
        }
        let mut nonce = [0u8; 16];
        getrandom::fill(&mut nonce)
            .map_err(|_| anyhow::anyhow!("Could not create update directory"))?;
        let directory = std::env::temp_dir().join(format!(
            "lighttable-update-{}",
            nonce.iter().map(|b| format!("{b:02x}")).collect::<String>()
        ));
        fs::create_dir(&directory)?;
        let helper = directory.join("LightTable-update.exe");
        let payload = directory.join("setup.exe");
        let ready = directory.join("ready");
        fs::copy(executable, &helper)?;
        fs::copy(installer, &payload)?;
        let mut child = Command::new(&helper)
            .arg("--apply-windows-update")
            .arg(&payload)
            .arg(executable)
            .arg(std::process::id().to_string())
            .arg(server_pid.to_string())
            .arg(&ready)
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if ready.is_file() {
                return Ok(directory);
            }
            if child.try_wait()?.is_some() {
                bail!("The update helper could not start");
            }
            thread::sleep(Duration::from_millis(20));
        }
        // Only the idle helper may be stopped; never stop the GUI or server.
        let _ = child.kill();
        let _ = child.wait();
        bail!("The update helper did not become ready")
    }

    struct ProcessHandle(*mut c_void);
    impl Drop for ProcessHandle {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
    impl ProcessHandle {
        fn open(pid: u32) -> Result<Self> {
            if pid == 0 {
                bail!("Invalid update process");
            }
            let handle = unsafe { OpenProcess(0x00100000, 0, pid) };
            if handle.is_null() {
                bail!("An update process is unavailable");
            }
            Ok(Self(handle))
        }
        fn wait(&self, timeout: Duration, directory: &Path) -> Result<()> {
            let deadline = Instant::now() + timeout;
            while Instant::now() < deadline {
                if directory.join("cancelled").is_file() {
                    bail!("The update was cancelled");
                }
                match unsafe { WaitForSingleObject(self.0, 25) } {
                    0 => return Ok(()),
                    258 => {} // WAIT_TIMEOUT: keep waiting without touching the process.
                    _ => bail!("Could not wait for LightTable to finish"),
                }
            }
            bail!("LightTable is still running; installation was cancelled")
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn cancelled_wait_never_terminates_the_target_process() {
            let directory = std::env::temp_dir().join(format!(
                "lighttable-update-cancel-test-{}",
                std::process::id()
            ));
            fs::create_dir_all(&directory).unwrap();
            fs::write(directory.join("cancelled"), b"cancelled").unwrap();
            let process = ProcessHandle::open(std::process::id()).unwrap();
            assert!(
                process
                    .wait(Duration::from_millis(100), &directory)
                    .is_err()
            );
            assert_eq!(unsafe { WaitForSingleObject(process.0, 0) }, 258);
            fs::remove_dir_all(directory).unwrap();
        }

        #[test]
        fn pinned_dll_exports_and_signing_key_are_usable() {
            let Some(path) = std::env::var_os("LIGHTTABLE_TEST_WINSPARKLE_DLL") else {
                return;
            };
            let wide: Vec<u16> = path.encode_wide().chain(Some(0)).collect();
            let module = unsafe {
                LoadLibraryExW(wide.as_ptr(), std::ptr::null_mut(), 0x00000100 | 0x00001000)
            };
            assert!(
                !module.is_null(),
                "WinSparkle must load from the staged payload"
            );
            let address =
                unsafe { GetProcAddress(module, c"win_sparkle_set_eddsa_public_key".as_ptr()) };
            assert!(!address.is_null());
            let key: unsafe extern "C" fn(*const c_char) -> i32 =
                unsafe { std::mem::transmute(address) };
            assert_eq!(
                unsafe { key(CString::new(PUBLIC_KEY).unwrap().as_ptr()) },
                1
            );
            assert_eq!(unsafe { key(c"invalid-key".as_ptr()) }, 0);
            for name in [
                "win_sparkle_set_can_shutdown_callback",
                "win_sparkle_set_user_run_installer_callback",
                "win_sparkle_set_shutdown_request_callback",
            ] {
                assert!(
                    !unsafe { GetProcAddress(module, CString::new(name).unwrap().as_ptr()) }
                        .is_null()
                );
            }
            unsafe {
                FreeLibrary(module);
            }
        }
    }

    pub fn helper(arguments: &[OsString]) -> Result<bool> {
        if arguments
            .first()
            .is_none_or(|value| value != "--apply-windows-update")
        {
            return Ok(false);
        }
        if arguments.len() != 6 {
            bail!("Invalid update helper arguments");
        }
        let installer = PathBuf::from(&arguments[1]);
        let executable = PathBuf::from(&arguments[2]);
        let own_directory = std::env::current_exe()?
            .parent()
            .context("Missing helper directory")?
            .to_owned();
        if installer != own_directory.join("setup.exe")
            || Path::new(&arguments[5]) != own_directory.join("ready")
            || !executable.is_absolute()
            || executable
                .file_name()
                .is_none_or(|name| !name.eq_ignore_ascii_case("LightTable.exe"))
        {
            bail!("Invalid update helper paths");
        }
        let installation = executable
            .parent()
            .context("Missing installation directory")?;
        if status(installation)["supported"] != true {
            bail!("This installation does not allow direct updates");
        }
        let parse_pid = |arg: &OsString| -> Result<u32> {
            Ok(arg.to_str().context("Invalid process ID")?.parse()?)
        };
        let app = ProcessHandle::open(parse_pid(&arguments[3])?)?;
        let server = ProcessHandle::open(parse_pid(&arguments[4])?)?;
        fs::File::create(&arguments[5])?.write_all(b"ready")?;
        // Handles identify the exact processes, even if Windows reuses a PID.
        app.wait(Duration::from_secs(120), &own_directory)?;
        server.wait(Duration::from_secs(30), &own_directory)?;
        if !own_directory.join("authorized").is_file() || own_directory.join("cancelled").is_file()
        {
            bail!("LightTable did not authorize installation after saving and closing");
        }
        let result = Command::new(&installer)
            .arg("/S")
            // NSIS requires /D to be the last argument, without manual quoting.
            .raw_arg(format!("/D={}", installation.display()))
            .status()?;
        if !result.success() {
            bail!("The LightTable installer failed");
        }
        Command::new(&executable)
            .current_dir(installation)
            .spawn()?;
        let _ = fs::remove_file(&installer);
        let _ = fs::remove_file(&arguments[5]);
        let _ = fs::remove_file(own_directory.join("authorized"));
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_signed_direct_installs_can_replace_application_files() {
        for channel in ["portable", "scoop", "winget", "chocolatey", "", "unknown"] {
            assert_eq!(
                channel_status(channel, true)["supported"],
                false,
                "{channel}"
            );
        }
        assert_eq!(channel_status("direct", false)["supported"], false);
        assert_eq!(channel_status("direct", true)["supported"], true);
    }
    #[test]
    fn powershell_markers_and_manifest_bom_keep_ownership_explicit() {
        let directory = std::env::temp_dir().join(format!(
            "lighttable-update-policy-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        std::fs::write(
            directory.join("build-manifest.json"),
            b"\xef\xbb\xbf{\"version\":\"1.2.3\",\"authenticode_signed\":true}\r\n",
        )
        .unwrap();
        std::fs::write(directory.join("install-channel.txt"), "\u{feff}direct\r\n").unwrap();
        assert_eq!(status(&directory)["supported"], true);
        std::fs::write(directory.join("install-channel.txt"), "winget\r\n").unwrap();
        assert_eq!(status(&directory)["supported"], false);
        assert_eq!(status(&directory)["managedBy"], "WinGet");
        std::fs::remove_file(directory.join("install-channel.txt")).unwrap();
        assert_eq!(status(&directory)["managedBy"], "portable");
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn unknown_installations_default_to_portable_and_feed_is_platform_specific() {
        assert_eq!(
            status(Path::new("/nonexistent-lighttable-install"))["managedBy"],
            "portable"
        );
        assert!(FEED_URL.starts_with("https://"));
        assert!(FEED_URL.ends_with("appcast-windows-x64.xml"));
        use base64::{Engine as _, engine::general_purpose::STANDARD};
        assert_eq!(STANDARD.decode(PUBLIC_KEY).unwrap().len(), 32);
    }
}
