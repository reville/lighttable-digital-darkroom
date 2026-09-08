pub mod preset_links;

use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

/// Export data arrives as UTF-8 for recipes and base64 for submission ZIPs.
/// Its destination remains exclusively the native Save dialog selection.
pub fn preset_export_data(content: &str, encoding: Option<&str>) -> Result<Vec<u8>> {
    use base64::{Engine as _, engine::general_purpose::STANDARD};
    if content.len() > 15 * 1024 * 1024 {
        bail!("The preset export exceeds 15 MB")
    }
    match encoding.unwrap_or("utf8") {
        "utf8" | "utf-8" => Ok(content.as_bytes().to_vec()),
        "base64" => STANDARD.decode(content).context("The preset export contains invalid base64 data"),
        _ => bail!("The preset export uses an unsupported encoding"),
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FolderSource {
    pub path: PathBuf,
    #[serde(default)]
    pub favorite: bool,
}

/// Logical window geometry remembered between launches.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct WindowState {
    pub width: f64,
    pub height: f64,
    #[serde(default)]
    pub maximized: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Settings {
    pub active: Option<PathBuf>,
    #[serde(default)]
    pub sources: Vec<FolderSource>,
    #[serde(default)]
    pub window: Option<WindowState>,
}

impl Settings {
    pub fn load(path: &Path) -> Self {
        fs::read(path)
            .ok()
            .and_then(|content| serde_json::from_slice(&content).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        let parent = path.parent().context("settings path has no parent")?;
        fs::create_dir_all(parent)?;
        let content = serde_json::to_vec_pretty(self)?;
        #[cfg(target_os = "windows")]
        fs::write(path, content)?;
        #[cfg(not(target_os = "windows"))]
        {
            let temporary = path.with_extension("tmp");
            fs::write(&temporary, content)?;
            fs::rename(temporary, path)?;
        }
        Ok(())
    }

    pub fn add_source(&mut self, path: PathBuf, favorite: bool) {
        let path = normalise(path);
        if let Some(source) = self.sources.iter_mut().find(|source| source.path == path) {
            source.favorite |= favorite;
        } else {
            self.sources.push(FolderSource { path, favorite });
            self.sources.sort_by(|left, right| {
                left.path
                    .to_string_lossy()
                    .to_lowercase()
                    .cmp(&right.path.to_string_lossy().to_lowercase())
            });
        }
    }

    pub fn toggle_favorite(&mut self, path: &Path) {
        let path = normalise(path.to_path_buf());
        if let Some(source) = self.sources.iter_mut().find(|source| source.path == path) {
            source.favorite = !source.favorite;
        }
    }

    pub fn remove_source(&mut self, path: &Path) {
        let path = normalise(path.to_path_buf());
        if self.sources.len() > 1 {
            self.sources.retain(|source| source.path != path);
        }
        if self.active.as_ref() == Some(&path) {
            self.active = self
                .sources
                .iter()
                .find(|source| source.path.is_dir())
                .map(|source| source.path.clone());
        }
    }

    pub fn remap_root(&mut self, old_root: &Path, new_root: &Path) {
        for source in &mut self.sources {
            if source.path == old_root {
                source.path = new_root.to_path_buf();
            } else if let Ok(suffix) = source.path.strip_prefix(old_root) {
                source.path = new_root.join(suffix);
            }
        }
        if self.active.as_ref() == Some(&old_root.to_path_buf()) {
            self.active = Some(new_root.to_path_buf());
        }
    }

    pub fn payload(&self, active: &Path) -> serde_json::Value {
        serde_json::json!({
            "type": "sources",
            "active": active.to_string_lossy(),
            "sources": self.sources.iter().map(|source| serde_json::json!({
                "path": source.path.to_string_lossy(),
                "name": source.path.file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| source.path.to_string_lossy().into_owned()),
                "favorite": source.favorite,
                "available": source.path.is_dir(),
            })).collect::<Vec<_>>(),
        })
    }
}

pub fn normalise(path: PathBuf) -> PathBuf {
    path.canonicalize().unwrap_or(path)
}

/// Threads for the server's OpenMP, numba, and BLAS pools.
///
/// The macOS host fixes these at four. Windows desktops commonly expose eight
/// to sixteen logical processors, so RAW decoding and export workers may take
/// up to half of them while the webview, the resident GPU engine, and the
/// server's own threads keep the rest. Small machines keep the proven four.
pub fn worker_threads(logical_processors: usize) -> usize {
    (logical_processors / 2).clamp(4, 8)
}

/// Fit a requested logical window size inside the monitor's work area.
///
/// A remembered or default size larger than the current display would open
/// with its frame off screen. The margin keeps the title bar and taskbar
/// reachable; the minimum still wins on displays smaller than the UI.
pub fn fit_window(requested: (f64, f64), minimum: (f64, f64), available: (f64, f64)) -> (f64, f64) {
    // Monitor size, not work area: leave room for the frame and a taskbar.
    const MARGIN: (f64, f64) = (24.0, 72.0);
    (
        requested.0.min(available.0 - MARGIN.0).max(minimum.0),
        requested.1.min(available.1 - MARGIN.1).max(minimum.1),
    )
}

pub fn source_folders(files: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut seen = HashSet::new();
    files
        .into_iter()
        .filter_map(|path| path.parent().map(Path::to_path_buf))
        .map(normalise)
        .filter(|path| seen.insert(path.clone()))
        .collect()
}

pub fn validate_windows_folder_name(raw: &str) -> Result<String> {
    let name = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    if name.is_empty()
        || matches!(name.as_str(), "." | "..")
        || name.starts_with('.')
        || name.ends_with(['.', ' '])
        || name.chars().any(char::is_control)
        || name
            .chars()
            .any(|character| "<>:\"/\\|?*".contains(character))
    {
        bail!("Enter a valid visible folder name")
    }
    let stem = name.split('.').next().unwrap_or_default().to_uppercase();
    let reserved = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (stem.len() == 4
            && matches!(&stem[..3], "COM" | "LPT")
            && stem.as_bytes()[3].is_ascii_digit()
            && stem.as_bytes()[3] != b'0');
    if reserved {
        bail!("That folder name is reserved by Windows")
    }
    Ok(name.chars().take(120).collect())
}

pub fn rename_root(path: &Path, raw_name: &str) -> Result<PathBuf> {
    let name = validate_windows_folder_name(raw_name)?;
    let parent = path.parent().context("the source folder has no parent")?;
    let destination = parent.join(name);
    if destination.exists() {
        bail!("A folder with that name already exists")
    }
    fs::rename(path, &destination)?;
    Ok(destination)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exported_recipes_and_binary_submissions_keep_their_exact_bytes() {
        assert_eq!(preset_export_data("{\"name\":\"Café\"}", None).unwrap(), "{\"name\":\"Café\"}".as_bytes());
        assert_eq!(preset_export_data("UEsDBAD/", Some("base64")).unwrap(), vec![80, 75, 3, 4, 0, 255]);
        for invalid in ["not base64!", "UEsDBAD_", "AA", "AB==", "AA==\n"] {
            assert!(preset_export_data(invalid, Some("base64")).is_err(), "{invalid:?}");
        }
        assert!(preset_export_data("text", Some("hex")).is_err());
        assert!(preset_export_data(&"A".repeat(15 * 1024 * 1024 + 1), Some("base64")).is_err());
    }

    #[test]
    fn settings_deduplicate_sources_and_preserve_favorites() {
        let mut settings = Settings::default();
        settings.add_source(PathBuf::from("photos"), false);
        settings.add_source(PathBuf::from("photos"), true);
        assert_eq!(settings.sources.len(), 1);
        assert!(settings.sources[0].favorite);
    }

    #[test]
    fn source_files_collapse_to_unique_parent_folders() {
        let folders = source_folders(vec![
            PathBuf::from("roll/a.jpg"),
            PathBuf::from("roll/b.jpg"),
            PathBuf::from("other/c.jpg"),
        ]);
        assert_eq!(folders, vec![PathBuf::from("roll"), PathBuf::from("other")]);
    }

    #[test]
    fn windows_names_reject_reserved_and_invalid_values() {
        for name in [
            "CON",
            "LPT1.txt",
            "photo?",
            "trailing.",
            ".hidden",
            "control\u{1}",
        ] {
            assert!(validate_windows_folder_name(name).is_err(), "{name}");
        }
        assert_eq!(
            validate_windows_folder_name("Scans  2026").unwrap(),
            "Scans 2026"
        );
    }

    #[test]
    fn worker_threads_scale_with_the_machine_but_stay_bounded() {
        assert_eq!(worker_threads(1), 4);
        assert_eq!(worker_threads(4), 4);
        assert_eq!(worker_threads(8), 4);
        assert_eq!(worker_threads(12), 6);
        assert_eq!(worker_threads(16), 8);
        assert_eq!(worker_threads(64), 8);
    }

    #[test]
    fn window_size_fits_the_work_area_and_respects_the_minimum() {
        let minimum = (1100.0, 700.0);
        assert_eq!(
            fit_window((1500.0, 950.0), minimum, (2560.0, 1400.0)),
            (1500.0, 950.0)
        );
        assert_eq!(
            fit_window((1500.0, 950.0), minimum, (1280.0, 800.0)),
            (1256.0, 728.0)
        );
        assert_eq!(
            fit_window((1500.0, 950.0), minimum, (1024.0, 600.0)),
            (1100.0, 700.0)
        );
    }

    #[test]
    fn window_state_round_trips_and_is_optional_for_older_settings() {
        let legacy: Settings = serde_json::from_str(r#"{"active":null,"sources":[]}"#).unwrap();
        assert_eq!(legacy.window, None);
        let mut settings = Settings::default();
        settings.window = Some(WindowState {
            width: 1440.0,
            height: 900.0,
            maximized: true,
        });
        let encoded = serde_json::to_string(&settings).unwrap();
        let decoded: Settings = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded.window, settings.window);
    }

    #[test]
    fn settings_can_be_saved_more_than_once() {
        let directory =
            std::env::temp_dir().join(format!("lighttable-settings-test-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("settings.json");
        let mut settings = Settings::default();
        settings.add_source(PathBuf::from("first"), true);
        settings.save(&path).unwrap();
        settings.add_source(PathBuf::from("second"), false);
        settings.save(&path).unwrap();
        assert_eq!(Settings::load(&path).sources.len(), 2);
        fs::remove_dir_all(directory).unwrap();
    }
}

/// Durable drafts live beside the catalog, independently of its Python writer.
/// Path components are SHA-256 identifiers supplied by the scoped UI adapter.
pub fn edit_recovery(root: &Path, body: &serde_json::Value) -> Result<serde_json::Value> {
    use std::io::Write;
    let id = |field: &str| -> Result<&str> {
        let value = body[field]
            .as_str()
            .context("missing recovery identifier")?;
        if value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            bail!("invalid recovery identifier")
        }
        Ok(value)
    };
    let directory = root.join(id("scope")?);
    let operation = body["operation"].as_str().unwrap_or_default();
    if operation == "list" {
        if !directory.exists() {
            return Ok(serde_json::json!([]));
        }
        let mut records = Vec::new();
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            if path.extension().and_then(|v| v.to_str()) == Some("json") {
                let record = (|| -> Result<serde_json::Value> {
                    Ok(serde_json::from_slice(&fs::read(&path)?)?)
                })();
                records.push(match record {
                    Ok(value) => value,
                    Err(error) => serde_json::json!({"journalError": error.to_string(),
                        "recordKey": path.file_name().unwrap_or_default().to_string_lossy()}),
                });
            }
        }
        return Ok(serde_json::Value::Array(records));
    }
    let path = directory.join(format!("{}.json", id("key")?));
    if operation == "remove" {
        if path.exists() {
            let previous: serde_json::Value = serde_json::from_slice(&fs::read(&path)?)?;
            if body["token"].is_string() && previous["token"] == body["token"] {
                fs::remove_file(&path)?;
                #[cfg(not(target_os = "windows"))]
                fs::File::open(&directory)?.sync_all()?;
            }
        }
        return Ok(serde_json::json!(true));
    }
    let record = &body["value"];
    if operation != "put"
        || !record["token"].is_string()
        || !record["name"].is_string()
        || !record["payload"].is_object()
    {
        bail!("invalid recovery record")
    }
    let bytes = serde_json::to_vec(record)?;
    if bytes.len() > 64 * 1024 * 1024 {
        bail!("edit recovery record exceeds 64 MB")
    }
    fs::create_dir_all(&directory)?;
    if path.exists() {
        // A damaged draft needs attention, never silently overwrite its bytes.
        serde_json::from_slice::<serde_json::Value>(&fs::read(&path)?)?;
    }
    let temporary = path.with_extension("pending");
    let result = (|| -> Result<()> {
        let mut file = fs::File::create(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, &path)?;
        #[cfg(not(target_os = "windows"))]
        fs::File::open(&directory)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result?;
    Ok(serde_json::json!(true))
}

#[cfg(test)]
mod recovery_tests {
    use super::*;
    use serde_json::json;
    fn root(suffix: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "lighttable-recovery-{}-{suffix}",
            std::process::id()
        ))
    }
    fn request(operation: &str, token: &str) -> serde_json::Value {
        json!({"operation": operation, "scope": "a".repeat(64), "key": "b".repeat(64),
               "token": token, "value": {"token": token, "name": "a.RAW", "payload": {
                   "state": {"grade": {"exposure": 2}, "masks": [{"data": [1, 2]}]}}}})
    }
    #[test]
    fn reopened_journal_keeps_new_revision_after_old_ack() {
        let directory = root("revisions");
        edit_recovery(&directory, &request("put", "one")).unwrap();
        edit_recovery(&directory, &request("put", "two")).unwrap();
        edit_recovery(&directory, &request("remove", "one")).unwrap();
        let records = edit_recovery(&directory, &request("list", "")).unwrap();
        assert_eq!(records.as_array().unwrap().len(), 1);
        assert_eq!(records[0]["token"], "two");
        edit_recovery(&directory, &request("remove", "two")).unwrap();
        assert_eq!(
            edit_recovery(&directory, &request("list", "")).unwrap(),
            json!([])
        );
        fs::remove_dir_all(directory).unwrap();
    }
    #[test]
    fn malformed_draft_is_not_overwritten_or_discarded() {
        let directory = root("corrupt");
        edit_recovery(&directory, &request("put", "one")).unwrap();
        let path = directory
            .join("a".repeat(64))
            .join(format!("{}.json", "b".repeat(64)));
        fs::write(&path, "{damaged").unwrap();
        assert!(
            edit_recovery(&directory, &request("list", "")).unwrap()[0]["journalError"].is_string()
        );
        assert!(edit_recovery(&directory, &request("put", "two")).is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), "{damaged");
        fs::remove_dir_all(directory).unwrap();
    }
    #[test]
    fn invalid_path_and_failed_writes_are_not_acknowledged() {
        let directory = root("failed");
        let mut packet = request("put", "one");
        packet["scope"] = json!("../escape");
        assert!(edit_recovery(&directory, &packet).is_err());
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("a".repeat(64)), "blocking file").unwrap();
        assert!(edit_recovery(&directory, &request("put", "one")).is_err());
        fs::remove_dir_all(directory).unwrap();
    }
}

/// Correlate native close replies across timeout, keep-open, and retry.
#[derive(Default)]
pub struct CloseAttempts {
    generation: u64,
    active: Option<u64>,
}
impl CloseAttempts {
    pub fn begin(&mut self) -> u64 {
        self.generation += 1;
        self.active = Some(self.generation);
        self.generation
    }
    pub fn accepts(&self, reply: Option<u64>) -> bool {
        self.active.is_some() && self.active == reply
    }
    pub fn cancel(&mut self) {
        self.active = None;
    }
}
#[cfg(test)]
mod close_tests {
    use super::CloseAttempts;
    #[test]
    fn timed_out_reply_cannot_close_a_newer_edit_session() {
        let mut attempts = CloseAttempts::default();
        let first = attempts.begin();
        attempts.cancel();
        assert!(!attempts.accepts(Some(first)));
        let second = attempts.begin();
        assert!(!attempts.accepts(Some(first)));
        assert!(!attempts.accepts(None));
        assert!(attempts.accepts(Some(second)));
        attempts.cancel();
        assert!(!attempts.accepts(Some(second)));
    }
}
