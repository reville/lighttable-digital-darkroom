#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

use lighttable_desktop_shell::CloseAttempts;

use std::{
    env,
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow, bail};
use directories::{BaseDirs, UserDirs};
use lighttable_desktop_shell::{Settings, edit_recovery, normalise, rename_root, source_folders};
use rfd::{FileDialog, MessageButtons, MessageDialog, MessageLevel};
use serde_json::{Value, json};
use tao::{
    dpi::LogicalSize,
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder},
    window::{Window, WindowBuilder},
};
use wry::{PageLoadEvent, WebView, WebViewBuilder};

fn photo_extensions() -> Vec<String> {
    let groups: Value = serde_json::from_str(include_str!("../../media-formats.json"))
        .expect("valid media-formats.json");
    ["raw", "processed", "video"]
        .into_iter()
        .flat_map(|group| {
            groups[group]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
        })
        .collect()
}

const BRIDGE_SCRIPT: &str = r#"
window.__LIGHTTABLE_PLATFORM__ = 'windows';
window.lightTableNativeBridge = {
  postMessage(message) { window.ipc.postMessage(JSON.stringify(message)); }
};
document.documentElement.classList.add('native-shell', 'windows-shell');
document.documentElement.style.setProperty('--native-window-controls-w', '0px');
"#;

#[derive(Debug)]
enum UserEvent {
    NativeMessage(String),
    EditJournalReply(Value),
    PageLoaded,
}

struct RuntimePaths {
    project: PathBuf,
    python: PathBuf,
    support: PathBuf,
    cache: PathBuf,
    settings: PathBuf,
    log: PathBuf,
}

impl RuntimePaths {
    fn discover() -> Result<Self> {
        let executable = env::current_exe()?;
        let executable_dir = executable.parent().context("executable has no parent")?;
        let project = env::var_os("LIGHTTABLE_PROJECT_DIR")
            .map(PathBuf::from)
            .or_else(|| {
                let bundled = executable_dir.join("Resources").join("LightTable");
                bundled.join("server.py").is_file().then_some(bundled)
            })
            .or_else(|| {
                let current = env::current_dir().ok()?;
                current.join("server.py").is_file().then_some(current)
            })
            .context("LightTable resources were not found")?;
        let python = [
            executable_dir.join("Python").join("python.exe"),
            project.join(".venv").join("Scripts").join("python.exe"),
            project.join(".venv").join("bin").join("python"),
        ]
        .into_iter()
        .find(|candidate| candidate.is_file())
        .context("the bundled Python runtime was not found")?;
        let base = BaseDirs::new().context("the local application-data folder is unavailable")?;
        let support = base.data_local_dir().join("LightTable");
        let cache = base.cache_dir().join("LightTable");
        Ok(Self {
            project,
            python,
            settings: support.join("desktop-settings.json"),
            log: support.join("server.log"),
            support,
            cache,
        })
    }
}

struct ServerController {
    child: Child,
    port: u16,
}

impl ServerController {
    fn start(paths: &RuntimePaths, folder: &Path) -> Result<Self> {
        fs::create_dir_all(&paths.support)?;
        fs::create_dir_all(&paths.cache)?;
        let port = choose_port()?;
        let log = std::fs::OpenOptions::new().create(true).append(true).open(&paths.log)?;
        let mut command = Command::new(&paths.python);
        command
            .arg(paths.project.join("server.py"))
            .current_dir(&paths.project)
            .env("LIGHTTABLE_DIR", folder)
            .env("LIGHTTABLE_PORT", port.to_string())
            .env("LIGHTTABLE_WATCH_PARENT", "1")
            .env("LIGHTTABLE_PARENT_PID", std::process::id().to_string())
            .env("LIGHTTABLE_CACHE_DIR", &paths.cache)
            .env("LIGHTTABLE_PREFS_FILE", paths.support.join("prefs.json"))
            .env(
                "LIGHTTABLE_PRESETS_FILE",
                paths.support.join("presets.json"),
            )
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("PYTHONPYCACHEPREFIX", paths.cache.join("python-bytecode"))
            .env("OMP_NUM_THREADS", "4")
            .env("NUMBA_NUM_THREADS", "4")
            .env("OPENBLAS_NUM_THREADS", "4")
            .env("PYTHONUNBUFFERED", "1")
            .env("LIGHTTABLE_LOG_FILE", &paths.log)
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log));

        let mut python_paths = vec![paths.project.join("vendor").join("spektrafilm").join("src")];
        if let Some(existing) = env::var_os("PYTHONPATH") {
            python_paths.extend(env::split_paths(&existing));
        }
        command.env("PYTHONPATH", env::join_paths(python_paths)?);

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000);
        }

        let child = command
            .spawn()
            .context("could not start the render server")?;
        let mut controller = Self { child, port };
        if let Err(error) = wait_until_ready(port, Duration::from_secs(45)) {
            controller.stop();
            return Err(error);
        }
        Ok(controller)
    }

    fn stop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

impl Drop for ServerController {
    fn drop(&mut self) {
        self.stop();
    }
}

struct AppState {
    window: Window,
    webview: WebView,
    server: ServerController,
    paths: RuntimePaths,
    settings: Settings,
    folder: PathBuf,
    journal: std::sync::mpsc::Sender<Value>,
    close_deadline: Option<Instant>,
    close_approved: bool,
    close_attempts: CloseAttempts,
}

impl AppState {
    fn send_event(&self, payload: Value) -> Result<()> {
        let encoded = serde_json::to_string(&payload)?;
        self.webview
            .evaluate_script(&format!("window.lightTableNativeEvent?.({encoded})"))?;
        Ok(())
    }

    fn finish_close(&mut self, saved: bool) {
        self.close_deadline = None;
        self.close_attempts.cancel();
        self.close_approved = saved || MessageDialog::new()
            .set_level(MessageLevel::Warning).set_title("Edits have not been saved")
            .set_description("Quit anyway? Keep the window open to retry saving. Available local recovery will be offered next time this catalog opens.")
            .set_buttons(MessageButtons::YesNo).show() == rfd::MessageDialogResult::Yes;
        if !self.close_approved {
            let _ = self.send_event(json!({"type": "closeCancelled"}));
        }
    }

    fn send_sources(&self) -> Result<()> {
        self.send_event(self.settings.payload(&self.folder))
    }

    fn save_settings(&self) -> Result<()> {
        self.settings.save(&self.paths.settings)
    }

    fn launch(&mut self, folder: PathBuf) -> Result<()> {
        let folder = normalise(folder);
        if !folder.is_dir() {
            bail!("That folder is no longer available")
        }
        self.settings
            .add_source(folder.clone(), self.settings.sources.is_empty());
        self.settings.active = Some(folder.clone());
        self.settings.save(&self.paths.settings)?;
        let server = ServerController::start(&self.paths, &folder)?;
        self.server.stop();
        self.server = server;
        self.folder = folder;
        self.window.set_title(&format!(
            "LightTable — {}",
            self.folder
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
        ));
        self.webview
            .load_url(&format!("http://127.0.0.1:{}/", self.server.port))?;
        Ok(())
    }

    fn handle_command(&mut self, raw: &str) -> Result<()> {
        let message: Value = serde_json::from_str(raw).context("invalid desktop message")?;
        let action = message
            .get("action")
            .and_then(Value::as_str)
            .unwrap_or_default();
        match action {
            "editJournal" => {
                self.journal
                    .send(message.clone())
                    .context("edit recovery worker stopped")?;
            }
            "closeReady" => {
                if self.close_deadline.is_some()
                    && self.close_attempts.accepts(message["attempt"].as_u64())
                {
                    self.finish_close(message["ok"].as_bool() == Some(true));
                }
            }
            "requestSources" => self.send_sources()?,
            "addPhotos" => {
                let extensions = photo_extensions();
                if let Some(files) = FileDialog::new()
                    .set_directory(&self.folder)
                    .add_filter("Photos", &extensions)
                    .pick_files()
                {
                    let folders = source_folders(files);
                    for folder in &folders {
                        self.settings.add_source(folder.clone(), false);
                    }
                    if let Some(folder) = folders.first() {
                        self.launch(folder.clone())?;
                    }
                }
            }
            "addFolder" => {
                if let Some(folder) = FileDialog::new().set_directory(&self.folder).pick_folder() {
                    self.launch(folder)?;
                }
            }
            "chooseCatalogFile" => {
                if let Some(file) = FileDialog::new()
                    .set_directory(&self.folder)
                    .add_filter("Catalogs", &["lrcat", "cocatalog"])
                    .pick_file()
                {
                    self.send_event(json!({
                        "type": "catalogFileSelected",
                        "path": file.to_string_lossy(),
                    }))?;
                }
            }
            "chooseIngestFolder" => {
                let field = message
                    .get("field")
                    .and_then(Value::as_str)
                    .unwrap_or("ingestSource")
                    .to_string();
                if let Some(folder) = FileDialog::new().pick_folder() {
                    self.send_event(json!({
                        "type": "ingestFolderSelected",
                        "field": field,
                        "path": folder.to_string_lossy(),
                    }))?;
                }
            }
            "listVolumes" => {
                self.send_event(json!({
                    "type": "volumes",
                    "volumes": removable_volumes(),
                }))?;
            }
            "listEditors" => {
                self.send_event(json!({"type": "editors", "editors": []}))?;
            }
            "chooseExternalEditor" => {
                if let Some(application) = FileDialog::new()
                    .add_filter("Applications", &["exe"])
                    .pick_file()
                {
                    let name = application
                        .file_stem()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .to_string();
                    self.send_event(json!({
                        "type": "editorChosen", "name": name,
                        "path": application.to_string_lossy(),
                    }))?;
                }
            }
            "openWith" => {
                let paths: Vec<String> = message
                    .get("paths")
                    .and_then(Value::as_array)
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(|value| value.as_str().map(str::to_owned))
                            .collect()
                    })
                    .unwrap_or_default();
                let application = message
                    .get("app")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if application.is_empty() {
                    for path in paths {
                        Command::new("cmd.exe")
                            .args(["/C", "start", "", &path])
                            .spawn()?;
                    }
                } else if !paths.is_empty() {
                    Command::new(application).args(paths).spawn()?;
                }
            }
            "trashFiles" => {
                // Rejected photographs go to the Recycle Bin, never an unlink,
                // so a mistaken cull stays recoverable.
                let paths: Vec<String> = message
                    .get("paths")
                    .and_then(Value::as_array)
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(|value| value.as_str().map(str::to_owned))
                            .collect()
                    })
                    .unwrap_or_default();
                let trashed = trash_paths(&paths);
                self.send_event(json!({"type": "trashed", "count": trashed}))?;
            }
            "selectSource" => {
                if let Some(path) = message.get("path").and_then(Value::as_str) {
                    self.launch(PathBuf::from(path))?;
                }
            }
            "toggleFavorite" => {
                if let Some(path) = message.get("path").and_then(Value::as_str) {
                    self.settings.toggle_favorite(Path::new(path));
                    self.save_settings()?;
                    self.send_sources()?;
                }
            }
            "removeSource" => {
                if let Some(path) = message.get("path").and_then(Value::as_str) {
                    let removed_active = normalise(PathBuf::from(path)) == self.folder;
                    self.settings.remove_source(Path::new(path));
                    self.save_settings()?;
                    if removed_active {
                        if let Some(next) = self.settings.active.clone() {
                            self.launch(next)?;
                        }
                    } else {
                        self.send_sources()?;
                    }
                }
            }
            "revealFolder" => {
                if let Some(path) = message.get("path").and_then(Value::as_str) {
                    reveal(Path::new(path))?;
                }
            }
            "renameRoot" => {
                if let (Some(path), Some(name)) = (
                    message.get("path").and_then(Value::as_str),
                    message.get("name").and_then(Value::as_str),
                ) {
                    let old_root = normalise(PathBuf::from(path));
                    let new_root = rename_root(&old_root, name)?;
                    self.settings.remap_root(&old_root, &new_root);
                    migrate_web_preferences(
                        &self.paths.support.join("prefs.json"),
                        &old_root,
                        &new_root,
                    )?;
                    self.launch(new_root)?;
                }
            }
            "refresh" => self.webview.reload()?,
            "savePreset" => {
                let filename = message
                    .get("filename")
                    .and_then(Value::as_str)
                    .unwrap_or("LightTable.ltpreset");
                let content = message
                    .get("content")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if let Some(destination) = FileDialog::new().set_file_name(filename).save_file() {
                    fs::write(&destination, content.as_bytes())?;
                    self.send_event(json!({
                        "type": "presetSaved",
                        "filename": destination.file_name()
                            .unwrap_or_default().to_string_lossy(),
                    }))?;
                }
            }
            _ => {}
        }
        Ok(())
    }
}

fn choose_port() -> Result<u16> {
    if TcpListener::bind(("127.0.0.1", 8321)).is_ok() {
        return Ok(8321);
    }
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

fn wait_until_ready(port: u16, timeout: Duration) -> Result<()> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if let Ok(mut stream) = TcpStream::connect_timeout(
            &format!("127.0.0.1:{port}").parse()?,
            Duration::from_secs(2),
        ) {
            stream.set_read_timeout(Some(Duration::from_secs(2)))?;
            stream.write_all(b"GET /api/images HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")?;
            let mut response = [0_u8; 32];
            if stream.read(&mut response).unwrap_or(0) > 0 && response.starts_with(b"HTTP/1.0 200")
            {
                return Ok(());
            }
        }
        thread::sleep(Duration::from_millis(200));
    }
    Err(anyhow!("the render server did not become ready"))
}

/// Removable drives, for the ingest dialog. Windows exposes these as drive
/// letters; elsewhere the conventional mount points are listed so the shell
/// still behaves during development on another platform.
fn removable_volumes() -> Vec<Value> {
    let mut volumes = Vec::new();
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        for letter in b'A'..=b'Z' {
            let root = format!("{}:\\", letter as char);
            let wide: Vec<u16> = std::ffi::OsStr::new(&root)
                .encode_wide()
                .chain(std::iter::once(0))
                .collect();
            const DRIVE_REMOVABLE: u32 = 2;
            let kind = unsafe { GetDriveTypeW(wide.as_ptr()) };
            if kind == DRIVE_REMOVABLE && Path::new(&root).is_dir() {
                volumes.push(json!({"path": root.clone(), "name": root}));
            }
        }
    }
    #[cfg(not(windows))]
    {
        for base in ["/Volumes", "/media", "/mnt"] {
            if let Ok(entries) = std::fs::read_dir(base) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.is_dir() {
                        volumes.push(json!({
                            "path": path.to_string_lossy(),
                            "name": entry.file_name().to_string_lossy(),
                        }));
                    }
                }
            }
        }
    }
    volumes
}

#[cfg(windows)]
unsafe extern "system" {
    fn GetDriveTypeW(root: *const u16) -> u32;
}

/// Move files to the Recycle Bin, reporting how many actually moved. Nothing
/// is unlinked: a mistaken cull has to stay recoverable, so a platform without
/// a recycle bin reports zero rather than deleting.
fn trash_paths(paths: &[String]) -> usize {
    paths
        .iter()
        .filter(|path| Path::new(path).exists())
        .filter(|path| recycle(Path::new(path)).is_ok())
        .count()
}

#[cfg(windows)]
fn recycle(path: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;

    #[repr(C)]
    struct ShFileOpStruct {
        hwnd: *mut core::ffi::c_void,
        func: u32,
        from: *const u16,
        to: *const u16,
        flags: u16,
        any_operations_aborted: i32,
        name_mappings: *mut core::ffi::c_void,
        progress_title: *const u16,
    }
    unsafe extern "system" {
        fn SHFileOperationW(op: *mut ShFileOpStruct) -> i32;
    }

    // SHFileOperationW with FOF_ALLOWUNDO is the documented route to the
    // Recycle Bin. The source string is double-null terminated.
    let mut wide: Vec<u16> = path.as_os_str().encode_wide().collect();
    wide.push(0);
    wide.push(0);
    const FO_DELETE: u32 = 0x0003;
    const FOF_SILENT: u16 = 0x0004;
    const FOF_NOCONFIRMATION: u16 = 0x0010;
    const FOF_ALLOWUNDO: u16 = 0x0040;
    let mut op = ShFileOpStruct {
        hwnd: std::ptr::null_mut(),
        func: FO_DELETE,
        from: wide.as_ptr(),
        to: std::ptr::null(),
        flags: FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT,
        any_operations_aborted: 0,
        name_mappings: std::ptr::null_mut(),
        progress_title: std::ptr::null(),
    };
    let status = unsafe { SHFileOperationW(&mut op) };
    if status == 0 {
        Ok(())
    } else {
        Err(anyhow!(
            "could not move {} to the Recycle Bin",
            path.display()
        ))
    }
}

#[cfg(not(windows))]
fn recycle(_path: &Path) -> Result<()> {
    Err(anyhow!("the Recycle Bin is only available on Windows"))
}

fn reveal(path: &Path) -> Result<()> {
    #[cfg(target_os = "windows")]
    let status = Command::new("explorer.exe")
        .arg(format!("/select,{}", path.display()))
        .status()?;
    #[cfg(target_os = "macos")]
    let status = Command::new("open").arg("-R").arg(path).status()?;
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let status = Command::new("xdg-open").arg(path).status()?;
    if !status.success() {
        bail!("the file browser could not open that location")
    }
    Ok(())
}

fn migrate_web_preferences(path: &Path, old_root: &Path, new_root: &Path) -> Result<()> {
    let Ok(content) = fs::read(path) else {
        return Ok(());
    };
    let mut preferences: Value = serde_json::from_slice(&content)?;
    let old = old_root.to_string_lossy();
    let new = new_root.to_string_lossy();
    if let Some(active) = preferences
        .get_mut("activeFolders")
        .and_then(Value::as_object_mut)
    {
        if let Some(value) = active.remove(old.as_ref()) {
            active.insert(new.to_string(), value);
        }
    }
    if let Some(favorites) = preferences
        .get_mut("favoriteFolders")
        .and_then(Value::as_array_mut)
    {
        for favorite in favorites {
            if let Some(value) = favorite.as_str() {
                if value == old || value.starts_with(&format!("{old}/")) {
                    *favorite = Value::String(format!("{new}{}", &value[old.len()..]));
                }
            }
        }
    }
    fs::write(path, serde_json::to_vec_pretty(&preferences)?)?;
    Ok(())
}

fn initial_folder(settings: &Settings) -> Option<PathBuf> {
    settings
        .active
        .as_ref()
        .filter(|path| path.is_dir())
        .cloned()
        .or_else(|| {
            settings
                .sources
                .iter()
                .find(|source| source.path.is_dir())
                .map(|source| source.path.clone())
        })
        .or_else(|| UserDirs::new()?.picture_dir().map(Path::to_path_buf))
        .filter(|path| path.is_dir())
}

fn show_fatal(message: &str) {
    let _ = MessageDialog::new()
        .set_level(MessageLevel::Error)
        .set_title("LightTable")
        .set_description(message)
        .set_buttons(MessageButtons::Ok)
        .show();
}

fn run() -> Result<()> {
    let paths = RuntimePaths::discover()?;
    let mut settings = Settings::load(&paths.settings);
    let folder = initial_folder(&settings)
        .or_else(|| {
            FileDialog::new()
                .set_title("Choose a photo folder")
                .pick_folder()
        })
        .context("no photo folder was chosen")?;
    settings.add_source(folder.clone(), settings.sources.is_empty());
    settings.active = Some(folder.clone());
    settings.save(&paths.settings)?;
    let server = ServerController::start(&paths, &folder)?;

    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let window = WindowBuilder::new()
        .with_title(format!(
            "LightTable — {}",
            folder.file_name().unwrap_or_default().to_string_lossy()
        ))
        .with_inner_size(LogicalSize::new(1500.0, 950.0))
        .with_min_inner_size(LogicalSize::new(1100.0, 700.0))
        .build(&event_loop)?;

    let command_proxy = proxy.clone();
    let load_proxy = proxy.clone();
    let webview = WebViewBuilder::new()
        .with_url(format!("http://127.0.0.1:{}/", server.port))
        .with_initialization_script(BRIDGE_SCRIPT)
        .with_clipboard(true)
        .with_ipc_handler(move |request| {
            let _ = command_proxy.send_event(UserEvent::NativeMessage(request.body().clone()));
        })
        .with_on_page_load_handler(move |event, _| {
            if matches!(event, PageLoadEvent::Finished) {
                let _ = load_proxy.send_event(UserEvent::PageLoaded);
            }
        })
        .build(&window)?;

    let (journal_tx, journal_rx) = std::sync::mpsc::channel::<Value>();
    let journal_proxy = proxy.clone();
    let catalog_directory = env::var_os("LIGHTTABLE_CATALOG_FILE")
        .map(PathBuf::from)
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| paths.support.join("Catalog"));
    thread::spawn(move || {
        for body in journal_rx {
            let mut reply = json!({"type": "editJournalReply", "id": body["id"]});
            match edit_recovery(
                &catalog_directory.join("Recovery").join("EditDrafts"),
                &body,
            ) {
                Ok(value) => reply["result"] = value,
                Err(error) => reply["error"] = Value::String(format!("{error:#}")),
            }
            if journal_proxy
                .send_event(UserEvent::EditJournalReply(reply))
                .is_err()
            {
                break;
            }
        }
    });
    let mut app = AppState {
        window,
        webview,
        server,
        paths,
        settings,
        folder: normalise(folder),
        journal: journal_tx,
        close_deadline: None,
        close_approved: false,
        close_attempts: CloseAttempts::default(),
    };

    event_loop.run(move |event, _, control_flow| {
        *control_flow = app.close_deadline.map(ControlFlow::WaitUntil).unwrap_or(ControlFlow::Wait);
        match event {
            Event::UserEvent(UserEvent::NativeMessage(message)) => {
                if let Err(error) = app.handle_command(&message) {
                    let _ = app.send_event(json!({
                        "type": "error",
                        "message": format!("{error:#}"),
                    }));
                }
            }
            Event::UserEvent(UserEvent::EditJournalReply(reply)) => {
                let _ = app.send_event(reply);
            }
            Event::UserEvent(UserEvent::PageLoaded) => {
                let _ = app.send_sources();
            }
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                if app.close_deadline.is_none() {
                    app.close_deadline = Some(Instant::now() + Duration::from_secs(12));
                    let attempt = app.close_attempts.begin();
                    let script = "Promise.resolve(window.lightTablePrepareToClose?.() ?? true).then(ok => window.lightTableNativeBridge.postMessage({action:'closeReady',attempt:__ATTEMPT__,ok})).catch(() => window.lightTableNativeBridge.postMessage({action:'closeReady',attempt:__ATTEMPT__,ok:false}))".replace("__ATTEMPT__", &attempt.to_string());
                    let _ = app.webview.evaluate_script(&script);
                }
            }
            Event::MainEventsCleared => {
                if app.close_deadline.is_some_and(|limit| Instant::now() >= limit) {
                    app.finish_close(false);
                }
            }
            _ => {}
        }
        if app.close_approved {
            app.server.stop();
            *control_flow = ControlFlow::Exit;
        } else if let Some(deadline) = app.close_deadline {
            *control_flow = ControlFlow::WaitUntil(deadline);
        }
    });
}

fn main() {
    if let Err(error) = run() {
        show_fatal(&format!("{error:#}"));
        std::process::exit(1);
    }
}
