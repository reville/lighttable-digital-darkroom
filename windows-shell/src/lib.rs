use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FolderSource {
    pub path: PathBuf,
    #[serde(default)]
    pub favorite: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Settings {
    pub active: Option<PathBuf>,
    #[serde(default)]
    pub sources: Vec<FolderSource>,
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
                records.push(serde_json::from_slice::<serde_json::Value>(&fs::read(
                    path,
                )?)?);
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
        assert!(edit_recovery(&directory, &request("list", "")).is_err());
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
