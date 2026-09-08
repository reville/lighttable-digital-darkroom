#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

use lighttable_desktop_shell::{
    CloseAttempts,
    preset_links::{self, PresetLinkInbox},
    localization::{self, tr, tr_args},
};

use std::{
    env, fs,
    io::Write,
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow, bail};
use directories::{BaseDirs, UserDirs};
use lighttable_desktop_shell::{
    Settings, WindowState, edit_recovery, fit_window, normalise, rename_root, source_folders,
    worker_threads,
};
use rfd::{FileDialog, MessageButtons, MessageDialog, MessageLevel};
use serde_json::{Value, json};
#[cfg(target_os = "linux")]
use tao::platform::unix::{EventLoopBuilderExtUnix, WindowExtUnix};
use tao::{
    dpi::LogicalSize,
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy},
    window::{Theme, Window, WindowBuilder},
};
#[cfg(target_os = "linux")]
use wry::WebViewBuilderExtUnix;
use wry::{PageLoadEvent, WebContext, WebView, WebViewBuilder};
#[cfg(target_os = "windows")]
use wry::{Theme as WebViewTheme, WebViewBuilderExtWindows};

fn photo_extensions() -> Vec<String> {
    let groups: Value = serde_json::from_str(include_str!("../../media-formats.json"))
        .expect("valid media-formats.json");
    let extensions: Vec<String> = ["raw", "processed", "video"]
        .into_iter()
        .flat_map(|group| {
            groups[group]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
        })
        .collect();
    #[cfg(target_os = "linux")]
    {
        // Portal globs are case-sensitive; cameras commonly write .NEF/.ARW.
        // Bracket patterns also cover mixed-case extensions such as .Jpeg.
        return extensions
            .iter()
            .map(|extension| {
                extension
                    .chars()
                    .map(|c| {
                        if c.is_ascii_alphabetic() {
                            format!("[{c}{}]", c.to_ascii_uppercase())
                        } else {
                            c.to_string()
                        }
                    })
                    .collect()
            })
            .collect();
    }
    #[cfg(not(target_os = "linux"))]
    extensions
}

const BRIDGE_SCRIPT: &str = r#"
window.__LIGHTTABLE_PLATFORM__ = '__PLATFORM__';
window.lightTableNativeBridge = {
  postMessage(message) { window.ipc.postMessage(JSON.stringify(message)); }
};
document.documentElement.classList.add('native-shell', '__PLATFORM__-shell');
document.documentElement.style.setProperty('--native-window-controls-w', '0px');
"#;

fn bridge_script(platform: &str, languages: &[String]) -> Result<String> {
    let script = BRIDGE_SCRIPT.replace("__PLATFORM__", platform);
    let languages = serde_json::to_string(languages)?;
    Ok(format!("{script}\nwindow.__LIGHTTABLE_SYSTEM_LANGUAGES__={languages};"))
}

/// `--bg` from `web/style.css`. The window and the webview paint it before
/// the UI arrives, so a launch or a folder switch never flashes white.
const BACKGROUND: (u8, u8, u8, u8) = (0x12, 0x12, 0x12, 0xff);

/// Shown while the render server starts. The window opens immediately and
/// stays responsive; the real UI replaces this page once the server answers.
const LOADING_PAGE: &str = r#"<!doctype html>
<html><head><meta charset="utf-8"><title>LightTable</title><style>
html, body { margin: 0; height: 100%; background: #121212; color: #cfcfcf;
  font: 13px system-ui, "Segoe UI", sans-serif; }
main { height: 100%; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 14px; }
.spin { width: 22px; height: 22px; border: 2px solid #353535; border-top-color: #f0f0f0;
  border-radius: 50%; animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style></head>
<body><main><div class="spin"></div><div>{{STARTING_MESSAGE}}</div></main></body></html>
"#;

fn loading_page() -> String {
    let message = tr("Starting LightTable…").replace('&', "&amp;")
        .replace('<', "&lt;").replace('>', "&gt;");
    LOADING_PAGE.replace("{{STARTING_MESSAGE}}", &message)
}

const DEFAULT_WINDOW: (f64, f64) = (1500.0, 950.0);
#[cfg(not(target_os = "linux"))]
const MINIMUM_WINDOW: (f64, f64) = (1100.0, 700.0);
#[cfg(target_os = "linux")]
const MINIMUM_WINDOW: (f64, f64) = (800.0, 480.0);
const SERVER_READY_TIMEOUT: Duration = Duration::from_secs(45);
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

enum UserEvent {
    NativeMessage(String),
    EditJournalReply(Value),
    PageLoaded,
    PageStarted,
    PresetLink(String),
    ServerReady {
        generation: u64,
        folder: PathBuf,
        result: Result<ServerController>,
    },
}

#[derive(Clone)]
struct RuntimePaths {
    project: PathBuf,
    python: PathBuf,
    support: PathBuf,
    cache: PathBuf,
    settings: PathBuf,
    prefs: PathBuf,
    log: PathBuf,
}

impl RuntimePaths {
    fn discover() -> Result<Self> {
        let executable = env::current_exe()?;
        let executable_dir = executable.parent().context(tr("executable has no parent"))?;
        let project = env::var_os("LIGHTTABLE_PROJECT_DIR")
            .map(PathBuf::from)
            .or_else(|| {
                let bundled = executable_dir.join("Resources").join("LightTable");
                bundled.join("server.py").is_file().then_some(bundled)
            })
            .or_else(|| {
                let bundled = executable_dir.parent()?.join("Resources/LightTable");
                bundled.join("server.py").is_file().then_some(bundled)
            })
            .or_else(|| {
                let current = env::current_dir().ok()?;
                current.join("server.py").is_file().then_some(current)
            })
            .context(tr("LightTable resources were not found"))?;
        let base = BaseDirs::new().context(tr("the local application-data folder is unavailable"))?;
        #[cfg(not(target_os = "linux"))]
        let (support, config, cache, log) = {
            let support = base.data_local_dir().join("LightTable");
            (
                support.clone(),
                support.clone(),
                base.cache_dir().join("LightTable"),
                support.join("server.log"),
            )
        };
        #[cfg(target_os = "linux")]
        let (support, config, cache, log) = {
            let dirs =
                lighttable_desktop_shell::linux::Directories::resolve(base.home_dir(), |key| {
                    env::var_os(key)
                });
            (
                dirs.data,
                dirs.config,
                dirs.cache,
                dirs.state.join("logs/server.log"),
            )
        };
        let override_path = |key| {
            #[cfg(target_os = "linux")]
            return lighttable_desktop_shell::linux::override_path(
                base.home_dir(),
                env::var_os(key),
            );
            #[cfg(not(target_os = "linux"))]
            env::var_os(key).filter(|value| !value.is_empty()).map(PathBuf::from)
        };
        let cache = override_path("LIGHTTABLE_CACHE_DIR").unwrap_or(cache);
        let log = override_path("LIGHTTABLE_SERVER_LOG")
            .or_else(|| override_path("LIGHTTABLE_LOG_FILE"))
            .unwrap_or(log);
        let prefs =
            override_path("LIGHTTABLE_PREFS_FILE").unwrap_or_else(|| config.join("prefs.json"));
        localization::initialize(&project, &prefs);
        let python = [
            executable_dir.join("Python").join("python.exe"),
            executable_dir.join("../Python/bin/python3"),
            project.join(".venv").join("Scripts").join("python.exe"),
            project.join(".venv").join("bin").join("python"),
        ]
        .into_iter()
        .find(|candidate| candidate.is_file())
        .context(tr("the bundled Python runtime was not found"))?;
        Ok(Self {
            project,
            python,
            settings: config.join("desktop-settings.json"),
            prefs,
            log,
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
        if let Some(parent) = paths.log.parent() {
            fs::create_dir_all(parent)?;
        }
        let log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&paths.log)?;
        let bytecode = paths.cache.join("python-bytecode");
        let threads = worker_threads(
            thread::available_parallelism()
                .map(usize::from)
                .unwrap_or(4),
        )
        .to_string();
        let mut command = Command::new(&paths.python);
        command
            // The embedded runtime's `python313._pth` puts the interpreter in
            // isolated mode, which ignores every PYTHON* environment variable.
            // Unbuffered logging and the bytecode cache location therefore
            // travel as interpreter options; the variables below still serve
            // a development virtual environment.
            .arg("-u")
            .arg("-X")
            .arg(format!("pycache_prefix={}", bytecode.display()))
            .arg(paths.project.join("server.py"))
            .current_dir(&paths.project)
            .env("LIGHTTABLE_DIR", folder)
            .env("LIGHTTABLE_PORT", port.to_string())
            .env("LIGHTTABLE_WATCH_PARENT", "1")
            .env("LIGHTTABLE_PARENT_PID", std::process::id().to_string())
            .env("LIGHTTABLE_CACHE_DIR", &paths.cache)
            .env("LIGHTTABLE_PREFS_FILE", &paths.prefs)
            .env("LIGHTTABLE_SERVER_LOG", &paths.log)
            .env("PYTHONPYCACHEPREFIX", &bytecode)
            .env("OMP_NUM_THREADS", &threads)
            .env("NUMBA_NUM_THREADS", &threads)
            .env("OPENBLAS_NUM_THREADS", &threads)
            .env("PYTHONUNBUFFERED", "1")
            .env("LIGHTTABLE_LOG_FILE", &paths.log)
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log));
        #[cfg(not(target_os = "linux"))]
        {
            command.env(
                "LIGHTTABLE_PRESETS_FILE",
                paths.support.join("presets.json"),
            );
        }
        if env::var_os("NUMBA_CACHE_DIR").is_none() {
            // Compiled kernels otherwise land beside the shipped sources,
            // which the uninstaller never removes and a read-only install
            // cannot accept at all.
            command.env("NUMBA_CACHE_DIR", paths.cache.join("compiled-runtime"));
        }

        let mut python_paths = vec![paths.project.join("vendor").join("spektrafilm").join("src")];
        if let Some(existing) = env::var_os("PYTHONPATH") {
            python_paths.extend(env::split_paths(&existing));
        }
        command.env("PYTHONPATH", env::join_paths(python_paths)?);

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(CREATE_NO_WINDOW);
        }

        let child = command
            .spawn()
            .context(tr("could not start the render server"))?;
        let mut controller = Self { child, port };
        if let Err(error) = wait_until_ready(port, SERVER_READY_TIMEOUT) {
            controller.stop();
            return Err(error);
        }
        Ok(controller)
    }

    fn stop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            #[cfg(target_os = "linux")]
            {
                // Let Python checkpoint the catalog and record a clean exit.
                // Child::kill sends SIGKILL on Unix and skips both steps.
                // This child has not been reaped, so its PID cannot be reused.
                unsafe { libc::kill(self.child.id() as libc::pid_t, libc::SIGTERM) };
                let deadline = Instant::now() + Duration::from_secs(5);
                while Instant::now() < deadline {
                    if self.child.try_wait().ok().flatten().is_some() {
                        return;
                    }
                    thread::sleep(Duration::from_millis(25));
                }
            }
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
    _web_context: WebContext,
    server: Option<ServerController>,
    paths: RuntimePaths,
    settings: Settings,
    folder: PathBuf,
    journal: std::sync::mpsc::Sender<Value>,
    close_deadline: Option<Instant>,
    close_approved: bool,
    close_attempts: CloseAttempts,
    proxy: EventLoopProxy<UserEvent>,
    /// Identifies the server start whose result is still wanted.
    launch_generation: u64,
    /// A start is in flight; further folder choices wait in `queued`.
    pending: bool,
    queued: Option<PathBuf>,
    /// Where to return if the folder being opened cannot start a server.
    fallback: Option<PathBuf>,
    /// Shown as a toast once the next page finishes loading.
    pending_error: Option<String>,
    pending_preset_links: std::collections::VecDeque<String>,
    preset_links_ready: bool,
    preset_link_inbox: Option<PresetLinkInbox>,
}

impl AppState {
    fn send_event(&self, payload: Value) -> Result<()> {
        let encoded = serde_json::to_string(&payload)?;
        self.webview
            .evaluate_script(&format!("window.lightTableNativeEvent?.({encoded})"))?;
        Ok(())
    }

    fn open_preset(&mut self, id: String) {
        if !preset_links::valid_preset_id(&id) {
            return;
        }
        if self.pending_preset_links.len() == 8 {
            self.pending_preset_links.pop_front();
        }
        self.pending_preset_links.push_back(id);
        self.window.set_minimized(false);
        self.window.set_focus();
        self.deliver_preset_links();
    }

    fn deliver_preset_links(&mut self) {
        if !self.preset_links_ready {
            return;
        }
        while let Some(id) = self.pending_preset_links.front() {
            if self
                .send_event(json!({"type": "presetLink", "id": id}))
                .is_err()
            {
                break;
            }
            self.pending_preset_links.pop_front();
        }
    }

    fn finish_close(&mut self, saved: bool) {
        self.close_deadline = None;
        self.close_attempts.cancel();
        let mut keep_open = tr("Keep Open");
        let mut quit = tr("Quit Anyway");
        if keep_open == quit { keep_open = "Keep Open".into(); quit = "Quit Anyway".into(); }
        self.close_approved = saved || MessageDialog::new()
            .set_level(MessageLevel::Warning).set_title(tr("Edits have not been saved"))
            .set_description(tr("Quit anyway? Keep the window open to retry saving. Available local recovery will be offered next time this catalog opens."))
            .set_buttons(MessageButtons::OkCancelCustom(quit.clone(), keep_open)).show()
                == rfd::MessageDialogResult::Custom(quit);
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

    fn set_title(&self, folder: &Path) {
        self.window.set_title(&tr_args("LightTable — {name}", &[("name", folder.file_name().unwrap_or_default().to_string_lossy().into_owned())]));
    }

    fn launch(&mut self, folder: PathBuf) -> Result<()> {
        let folder = normalise(folder);
        if !folder.is_dir() {
            bail!(tr("That folder is no longer available"))
        }
        self.settings
            .add_source(folder.clone(), self.settings.sources.is_empty());
        self.settings.active = Some(folder.clone());
        self.settings.save(&self.paths.settings)?;
        if self.pending {
            // One server start at a time: the catalog lease belongs to the
            // process being started, so the newest choice waits its turn.
            self.queued = Some(folder);
            return Ok(());
        }
        let fallback = self.server.as_ref().map(|_| self.folder.clone());
        self.begin_server(folder, fallback);
        Ok(())
    }

    /// Stop the running server and start one for `folder` off the UI thread.
    ///
    /// One catalog holds one process lease, so the previous server must be
    /// gone before its replacement opens the same library. The loading page
    /// covers the gap while the window keeps painting, moving, and closing.
    fn begin_server(&mut self, folder: PathBuf, fallback: Option<PathBuf>) {
        if let Some(mut previous) = self.server.take() {
            previous.stop();
        }
        self.folder = folder.clone();
        self.preset_links_ready = false;
        self.fallback = fallback;
        self.set_title(&folder);
        let _ = self.webview.load_html(&loading_page());
        self.launch_generation += 1;
        self.pending = true;
        let generation = self.launch_generation;
        let paths = self.paths.clone();
        let proxy = self.proxy.clone();
        thread::spawn(move || {
            let result = ServerController::start(&paths, &folder);
            let _ = proxy.send_event(UserEvent::ServerReady {
                generation,
                folder,
                result,
            });
        });
    }

    fn server_ready(&mut self, generation: u64, folder: PathBuf, result: Result<ServerController>) {
        if generation != self.launch_generation {
            // A newer start superseded this one; its server must not linger.
            if let Ok(mut stale) = result {
                stale.stop();
            }
            return;
        }
        self.pending = false;
        if let Some(next) = self.queued.take() {
            if let Ok(mut superseded) = result {
                superseded.stop();
            }
            let fallback = self.fallback.take().or(Some(folder));
            self.begin_server(next, fallback);
            return;
        }
        match result {
            Ok(server) => {
                let url = format!("http://127.0.0.1:{}/", server.port);
                self.server = Some(server);
                self.fallback = None;
                if let Err(error) = self.webview.load_url(&url) {
                    show_fatal(&format!("{error:#}"));
                    std::process::exit(1);
                }
            }
            Err(error) => {
                let message = format!("{error:#}");
                match self
                    .fallback
                    .take()
                    .filter(|previous| previous != &folder && previous.is_dir())
                {
                    Some(previous) => {
                        // Return to the folder that was open, and say why.
                        self.pending_error = Some(tr_args("Could not open {name}: {message}", &[
                            ("name", folder.file_name().unwrap_or_default().to_string_lossy().into_owned()),
                            ("message", message),
                        ]));
                        self.settings.active = Some(previous.clone());
                        let _ = self.save_settings();
                        self.begin_server(previous, None);
                    }
                    None => {
                        show_fatal(&message);
                        std::process::exit(1);
                    }
                }
            }
        }
    }

    fn page_loaded(&mut self) {
        let _ = self.send_sources();
        if let Some(message) = self.pending_error.take() {
            let _ = self.send_event(json!({"type": "error", "message": message}));
        }
    }

    /// Remember the logical window size for the next launch. A maximized
    /// window keeps the last restored size so un-maximizing later is sane.
    fn remember_window(&mut self) {
        let maximized = self.window.is_maximized();
        let (width, height) = if maximized {
            self.settings
                .window
                .map(|state| (state.width, state.height))
                .unwrap_or(DEFAULT_WINDOW)
        } else {
            let size = self
                .window
                .inner_size()
                .to_logical::<f64>(self.window.scale_factor());
            (size.width, size.height)
        };
        self.settings.window = Some(WindowState {
            width,
            height,
            maximized,
        });
        let _ = self.save_settings();
    }

    fn handle_command(&mut self, raw: &str) -> Result<()> {
        let message: Value = serde_json::from_str(raw).context(tr("invalid desktop message"))?;
        let action = message
            .get("action")
            .and_then(Value::as_str)
            .unwrap_or_default();
        match action {
            "editJournal" => {
                self.journal
                    .send(message.clone())
                    .context(tr("edit recovery worker stopped"))?;
            }
            "closeReady" => {
                if self.close_deadline.is_some()
                    && self.close_attempts.accepts(message["attempt"].as_u64())
                {
                    self.finish_close(message["ok"].as_bool() == Some(true));
                }
            }
            "localizationChanged" => {
                localization::reload().map_err(|error| anyhow!(tr(&error)))?;
                self.set_title(&self.folder);
            }
            "requestSources" => self.send_sources()?,
            "requestPresetLinks" => {
                self.preset_links_ready = true;
                self.deliver_preset_links();
            }
            "addPhotos" => {
                let extensions = photo_extensions();
                if let Some(files) = FileDialog::new().set_title(tr("Add Photos"))
                    .set_parent(&self.window)
                    .set_directory(&self.folder)
                    .add_filter(&tr("Photos"), &extensions)
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
                if let Some(folder) = FileDialog::new().set_title(tr("Add a folder to LightTable"))
                    .set_parent(&self.window)
                    .set_directory(&self.folder)
                    .pick_folder()
                {
                    self.launch(folder)?;
                }
            }
            "chooseCatalogFile" => {
                if let Some(file) = FileDialog::new().set_title(tr("Choose a catalog"))
                    .set_parent(&self.window)
                    .set_directory(&self.folder)
                    .add_filter(&tr("Catalogs"), &["lrcat", "cocatalog"])
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
                if let Some(folder) = FileDialog::new().set_title(tr("Choose a folder")).set_parent(&self.window).pick_folder() {
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
                let dialog = FileDialog::new().set_title(tr("Choose an external editor")).set_parent(&self.window);
                #[cfg(target_os = "windows")]
                let dialog = dialog.add_filter(&tr("Applications"), &["exe"]);
                if let Some(application) = dialog.pick_file() {
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
                        open_with_default_application(&path)?;
                    }
                } else if !paths.is_empty() {
                    #[cfg(target_os = "linux")]
                    if Path::new(application)
                        .extension()
                        .is_some_and(|ext| ext == "desktop")
                    {
                        Command::new("gio")
                            .arg("launch")
                            .arg(application)
                            .args(paths)
                            .spawn()?;
                        return Ok(());
                    }
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
                    migrate_web_preferences(&self.paths.prefs, &old_root, &new_root)?;
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
                let data = lighttable_desktop_shell::preset_export_data(
                    content,
                    message.get("encoding").and_then(Value::as_str),
                )?;
                if let Some(destination) = FileDialog::new().set_title(tr("Export Preset"))
                    .set_parent(&self.window)
                    .set_file_name(filename)
                    .save_file()
                {
                    fs::write(&destination, data)?;
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
            write!(
                stream,
                "GET /api/health HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            )?;
            if lighttable_desktop_shell::successful_health_response(&mut stream) {
                return Ok(());
            }
        }
        thread::sleep(Duration::from_millis(200));
    }
    Err(anyhow!(tr("the render server did not become ready")))
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
    #[cfg(target_os = "linux")]
    {
        if let Ok(mountinfo) = fs::read_to_string("/proc/self/mountinfo") {
            for path in lighttable_desktop_shell::linux::media_mounts(&mountinfo) {
                if path.is_dir() {
                    volumes.push(json!({
                        "path": path.to_string_lossy(),
                        "name": path.file_name().unwrap_or_default().to_string_lossy(),
                    }));
                }
            }
        }
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
        Err(anyhow!(tr_args("Could not move {path} to the Recycle Bin", &[("path", path.display().to_string())])))
    }
}

#[cfg(target_os = "linux")]
fn recycle(path: &Path) -> Result<()> {
    if !path.is_absolute() {
        bail!(tr("Trash requires an absolute photo path"))
    }
    // GIO implements the freedesktop Trash specification, including metadata
    // required for Restore. Failure never falls through to permanent deletion.
    let result = Command::new("gio")
        .args(["trash", "--"])
        .arg(path)
        .output()
        .context(tr("The desktop Trash service (gio) is unavailable"))?;
    if !result.status.success() {
        bail!(tr_args(
            "Could not move {path} to Trash: {error}",
            &[("path", path.display().to_string()),
              ("error", String::from_utf8_lossy(&result.stderr).trim().to_owned())]
        ))
    }
    Ok(())
}

#[cfg(not(any(windows, target_os = "linux")))]
fn recycle(_path: &Path) -> Result<()> {
    Err(anyhow!(tr("the Recycle Bin is only available on Windows")))
}

/// Open a file with the application registered for it.
fn open_with_default_application(path: &str) -> Result<()> {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        // `cmd.exe` is a console program; without this flag a command window
        // would flash on every double-click from a shell that has no console.
        Command::new("cmd.exe")
            .args(["/C", "start", "", path])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()?;
    }
    #[cfg(target_os = "macos")]
    Command::new("open").arg(path).spawn()?;
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    Command::new("xdg-open").arg(path).spawn()?;
    Ok(())
}

fn reveal(path: &Path) -> Result<()> {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        // Explorer reports a nonzero exit status even after it has selected
        // the item, so only a failure to launch it is an error here.
        Command::new("explorer.exe")
            .raw_arg(format!("/select,\"{}\"", path.display()))
            .spawn()
            .context("File Explorer could not be started")?;
        return Ok(());
    }
    #[cfg(target_os = "macos")]
    let status = Command::new("open").arg("-R").arg(path).status()?;
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let status = Command::new("xdg-open")
        .arg(if path.is_dir() {
            path
        } else {
            path.parent().context(tr("the file has no parent folder"))?
        })
        .status()?;
    #[cfg(not(target_os = "windows"))]
    if !status.success() {
        bail!(tr("the file browser could not open that location"))
    }
    #[cfg(not(target_os = "windows"))]
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
    if let Some(folder) = env::var_os("LIGHTTABLE_DIR")
        .map(PathBuf::from)
        .filter(|p| p.is_dir())
    {
        return Some(folder);
    }
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
        .set_buttons(MessageButtons::OkCustom(tr("OK")))
        .show();
}

fn run() -> Result<()> {
    let arguments: Vec<String> = env::args().skip(1).collect();
    let initial_preset = match arguments.as_slice() {
        [] => None,
        [flag, url] if flag == "--preset-url" => Some(
            preset_links::preset_id(url)
                .context("invalid preset link")?
                .to_owned(),
        ),
        [url] => Some(
            preset_links::preset_id(url)
                .context("invalid preset link")?
                .to_owned(),
        ),
        _ => bail!("unsupported LightTable application arguments"),
    };
    let paths = RuntimePaths::discover()?;
    if initial_preset
        .as_deref()
        .is_some_and(|id| preset_links::forward(&paths.support, id))
    {
        return Ok(());
    }
    let mut event_builder = EventLoopBuilder::<UserEvent>::with_user_event();
    #[cfg(target_os = "linux")]
    {
        // GTK derives its X11 WM_CLASS from the program name. Keep it aligned
        // with the Wayland app ID and installed desktop entry for launch/focus.
        gtk::glib::set_prgname(Some("org.lighttable.LightTable"));
        gtk::glib::set_application_name("LightTable");
        event_builder.with_app_id("org.lighttable.LightTable");
    }
    let event_loop = event_builder.build();
    let proxy = event_loop.create_proxy();
    let link_proxy = proxy.clone();
    let preset_link_inbox = PresetLinkInbox::start(&paths.support, move |id| {
        link_proxy.send_event(UserEvent::PresetLink(id)).is_ok()
    })?;
    let mut settings = Settings::load(&paths.settings);
    let folder = initial_folder(&settings)
        .or_else(|| {
            FileDialog::new()
                .set_title(tr("Choose a photo folder"))
                .pick_folder()
        })
        .context(tr("no photo folder was chosen"))?;
    let folder = normalise(folder);
    settings.add_source(folder.clone(), settings.sources.is_empty());
    settings.active = Some(folder.clone());
    settings.save(&paths.settings)?;

    let available = event_loop
        .primary_monitor()
        .map(|monitor| {
            let size = monitor.size().to_logical::<f64>(monitor.scale_factor());
            (size.width, size.height)
        })
        .unwrap_or(DEFAULT_WINDOW);
    let remembered = settings.window;
    let (width, height) = fit_window(
        remembered
            .map(|state| (state.width, state.height))
            .unwrap_or(DEFAULT_WINDOW),
        MINIMUM_WINDOW,
        available,
    );
    // Hyprland manages tiling, borders and window actions. Retain normal GTK
    // decorations elsewhere and let GTK follow the desktop's light/dark theme.
    #[cfg(target_os = "linux")]
    let hyprland = lighttable_desktop_shell::linux::is_hyprland(|key| env::var_os(key));
    #[cfg(not(target_os = "linux"))]
    let hyprland = false;
    let window = WindowBuilder::new()
        .with_title(tr_args("LightTable — {name}", &[("name", folder.file_name().unwrap_or_default().to_string_lossy().into_owned())]))
        .with_inner_size(LogicalSize::new(width, height))
        .with_min_inner_size(LogicalSize::new(MINIMUM_WINDOW.0, MINIMUM_WINDOW.1))
        .with_maximized(!hyprland && remembered.is_some_and(|state| state.maximized))
        .with_decorations(!hyprland)
        .with_theme(if cfg!(target_os = "linux") {
            None
        } else {
            Some(Theme::Dark)
        })
        .with_background_color(BACKGROUND)
        .build(&event_loop)?;

    // WebView2 otherwise keeps its profile beside the executable, which the
    // uninstaller never removes and a read-only location cannot hold.
    let profile_name = if cfg!(target_os = "linux") {
        "WebKitGTK"
    } else {
        "WebView2"
    };
    let mut web_context = WebContext::new(Some(paths.support.join(profile_name)));
    let platform = if cfg!(target_os = "linux") {
        "linux"
    } else {
        "windows"
    };
    let bridge_script = bridge_script(platform, &localization::system_languages())?;
    let command_proxy = proxy.clone();
    let load_proxy = proxy.clone();
    let builder = WebViewBuilder::new_with_web_context(&mut web_context)
        .with_html(loading_page())
        .with_background_color(BACKGROUND)
        .with_initialization_script(&bridge_script)
        .with_clipboard(true)
        .with_ipc_handler(move |request| {
            let _ = command_proxy.send_event(UserEvent::NativeMessage(request.body().clone()));
        })
        .with_on_page_load_handler(move |event, _| {
            let _ = load_proxy.send_event(match event {
                PageLoadEvent::Started => UserEvent::PageStarted,
                PageLoadEvent::Finished => UserEvent::PageLoaded,
            });
        });
    #[cfg(target_os = "windows")]
    let builder = builder.with_theme(WebViewTheme::Dark);
    #[cfg(target_os = "linux")]
    let webview = builder.build_gtk(
        window
            .default_vbox()
            .context(tr("the GTK window container is unavailable"))?,
    )?;
    #[cfg(not(target_os = "linux"))]
    let webview = builder.build(&window)?;

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
        _web_context: web_context,
        server: None,
        paths,
        settings,
        folder: folder.clone(),
        journal: journal_tx,
        close_deadline: None,
        close_approved: false,
        close_attempts: CloseAttempts::default(),
        proxy,
        launch_generation: 0,
        pending: false,
        queued: None,
        fallback: None,
        pending_error: None,
        pending_preset_links: initial_preset.into_iter().collect(),
        preset_links_ready: false,
        preset_link_inbox: Some(preset_link_inbox),
    };
    app.begin_server(folder, None);

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
            Event::UserEvent(UserEvent::PageLoaded) => app.page_loaded(),
            Event::UserEvent(UserEvent::PageStarted) => app.preset_links_ready = false,
            Event::UserEvent(UserEvent::PresetLink(id)) => app.open_preset(id),
            Event::UserEvent(UserEvent::ServerReady {
                generation,
                folder,
                result,
            }) => app.server_ready(generation, folder, result),
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                app.remember_window();
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
            app.preset_link_inbox.take();
            if let Some(mut server) = app.server.take() {
                server.stop();
            }
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

#[cfg(test)]
mod bridge_tests {
    use super::bridge_script;

    #[test]
    fn platform_and_language_values_survive_bridge_initialization() {
        let languages = vec!["es-MX".to_owned(), "quote\"\nvalue".to_owned()];
        for platform in ["windows", "linux"] {
            let script = bridge_script(platform, &languages).unwrap();
            assert!(script.contains(&format!("window.__LIGHTTABLE_PLATFORM__ = '{platform}'")));
            assert!(script.contains(&format!("'{platform}-shell'")));
            assert!(!script.contains("__PLATFORM__"));
            let serialized = script.split("window.__LIGHTTABLE_SYSTEM_LANGUAGES__=")
                .nth(1).unwrap().strip_suffix(';').unwrap();
            assert_eq!(serde_json::from_str::<Vec<String>>(serialized).unwrap(), languages);
        }
    }
}
