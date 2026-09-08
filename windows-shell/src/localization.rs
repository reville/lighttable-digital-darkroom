//! Native UI shares the web manifest, catalogs, and saved preference object.
use serde::Deserialize;
use serde_json::Value;
use std::{
    collections::{BTreeSet, HashMap},
    env, fs,
    path::{Path, PathBuf},
    sync::{OnceLock, RwLock},
};
#[derive(Clone, Deserialize)]
pub struct Language {
    pub code: String,
    pub name: String,
    #[serde(rename = "nativeName")]
    pub native_name: String,
    pub dir: String,
}
#[derive(Deserialize)]
struct Manifest {
    version: u32,
    #[serde(rename = "sourceLocale")]
    source_locale: String,
    locales: Vec<Language>,
}
#[derive(Deserialize)]
struct Catalog {
    version: u32,
    locale: String,
    messages: HashMap<String, String>,
}
pub struct NativeLocaleStore {
    directory: PathBuf,
    preferences: PathBuf,
    pub languages: Vec<Language>,
    pub locale: String,
    messages: HashMap<String, String>,
}
pub fn safe_code(code: &str) -> bool {
    let mut parts = code.split('-');
    let first = parts.next().unwrap_or_default();
    (2..=3).contains(&first.len())
        && first.bytes().all(|c| c.is_ascii_alphabetic())
        && parts.all(|part| {
            (2..=8).contains(&part.len()) && part.bytes().all(|c| c.is_ascii_alphanumeric())
        })
}
fn placeholders(text: &str) -> BTreeSet<&str> {
    text.split('{')
        .skip(1)
        .filter_map(|part| part.split_once('}').map(|p| p.0))
        .filter(|key| {
            !key.is_empty()
                && key.as_bytes()[0].is_ascii_alphabetic()
                && key.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
        })
        .collect()
}
impl NativeLocaleStore {
    pub fn new(directory: PathBuf, preferences: PathBuf) -> Self {
        let manifest = fs::read(directory.join("manifest.json"))
            .ok()
            .and_then(|data| serde_json::from_slice::<Manifest>(&data).ok());
        let mut seen = BTreeSet::new();
        let mut languages: Vec<Language> = manifest
            .filter(|m| m.version == 1 && m.source_locale == "en")
            .map(|m| {
                m.locales
                    .into_iter()
                    .filter(|e| {
                        safe_code(&e.code)
                            && !e.native_name.is_empty()
                            && matches!(e.dir.as_str(), "ltr" | "rtl")
                            && seen.insert(e.code.clone())
                    })
                    .collect()
            })
            .unwrap_or_default();
        if !languages.iter().any(|e| e.code == "en") {
            languages = vec![Language {
                code: "en".into(),
                name: "English".into(),
                native_name: "English".into(),
                dir: "ltr".into(),
            }];
        }
        let mut store = Self {
            directory,
            preferences,
            languages,
            locale: "en".into(),
            messages: HashMap::new(),
        };
        let _ = store.reload();
        store
    }
    pub fn supported(&self, code: &str) -> Option<String> {
        self.languages
            .iter()
            .find(|e| e.code.eq_ignore_ascii_case(code))
            .map(|e| e.code.clone())
    }
    pub fn suggested(&self, language: &str) -> String {
        let tag = language.replace('_', "-");
        let tag = tag.split('.').next().unwrap_or(&tag);
        if let Some(exact) = self.supported(tag) {
            return exact;
        }
        let tag = tag.to_ascii_lowercase();
        let pieces: Vec<&str> = tag.split('-').collect();
        if pieces.first() == Some(&"zh") {
            let traditional = pieces.contains(&"hant")
                || (!pieces.contains(&"hans")
                    && pieces.iter().any(|p| ["tw", "hk", "mo"].contains(p)));
            if let Some(code) = self.supported(if traditional { "zh-Hant" } else { "zh-Hans" }) {
                return code;
            }
        }
        self.supported(pieces.first().copied().unwrap_or("en"))
            .unwrap_or_else(|| "en".into())
    }
    pub fn set_locale(&mut self, code: &str) {
        self.locale = self.supported(code).unwrap_or_else(|| "en".into());
        self.messages.clear();
        if self.locale == "en" {
            return;
        }
        let catalog = fs::read(self.directory.join(format!("{}.json", self.locale)))
            .ok()
            .and_then(|data| serde_json::from_slice::<Catalog>(&data).ok());
        if let Some(c) = catalog.filter(|c| c.version == 1 && c.locale == self.locale) {
            self.messages = c
                .messages
                .into_iter()
                .filter(|(source, translated)| {
                    !translated.trim().is_empty()
                        && placeholders(source) == placeholders(translated)
                })
                .collect();
        }
    }
    pub fn reload(&mut self) -> Result<(), String> {
        let prefs = match fs::read(&self.preferences) {
            Ok(data) => serde_json::from_slice::<Value>(&data)
                .map_err(|_| "The preferences file could not be read.")?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => serde_json::json!({}),
            Err(_) => return Err("The preferences file could not be read.".into()),
        };
        if !prefs.is_object() {
            return Err("The preferences file is not a JSON object.".into());
        }
        let code = self
            .supported(prefs["locale"].as_str().unwrap_or_default())
            .unwrap_or_else(|| "en".into());
        self.set_locale(&code);
        Ok(())
    }
    pub fn text(&self, source: &str, arguments: &[(&str, String)]) -> String {
        let template = self
            .messages
            .get(source)
            .map(String::as_str)
            .unwrap_or(source);
        substitute(template, arguments)
    }
}
fn substitute(template: &str, arguments: &[(&str, String)]) -> String {
    let mut result = String::new();
    let mut rest = template;
    while let Some(open) = rest.find('{') {
        result.push_str(&rest[..open]);
        rest = &rest[open..];
        if let Some(close) = rest.find('}') {
            let key = &rest[1..close];
            if let Some((_, value)) = arguments.iter().find(|(name, _)| *name == key) {
                result.push_str(value);
            } else {
                result.push_str(&rest[..=close]);
            }
            rest = &rest[close + 1..];
        } else {
            result.push_str(rest);
            return result;
        }
    }
    result.push_str(rest);
    result
}

static STORE: OnceLock<RwLock<NativeLocaleStore>> = OnceLock::new();
/// Use the same resolved preferences file as the server on every desktop.
pub fn initialize(project: &Path, preferences: &Path) {
    let _ = STORE.set(RwLock::new(NativeLocaleStore::new(
        project.join("web/locales"),
        preferences.to_path_buf(),
    )));
}
pub fn reload() -> Result<(), String> {
    STORE
        .get()
        .ok_or("Language settings are unavailable.")?
        .write()
        .map_err(|_| "Language settings are unavailable.")?
        .reload()
}
pub fn tr(source: &str) -> String {
    tr_args(source, &[])
}
pub fn tr_args(source: &str, arguments: &[(&str, String)]) -> String {
    STORE
        .get()
        .and_then(|s| s.read().ok())
        .map(|s| s.text(source, arguments))
        .unwrap_or_else(|| substitute(source, arguments))
}
/// Read the actual Windows UI-language list without running a shell.
#[cfg(windows)]
pub fn system_languages() -> Vec<String> {
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetUserPreferredUILanguages(
            flags: u32,
            count: *mut u32,
            buffer: *mut u16,
            size: *mut u32,
        ) -> i32;
        fn GetUserDefaultLocaleName(buffer: *mut u16, size: i32) -> i32;
    }
    let mut count = 0;
    let mut size = 0;
    let measured =
        unsafe { GetUserPreferredUILanguages(0x8, &mut count, std::ptr::null_mut(), &mut size) };
    if measured != 0 && (2..=16_384).contains(&size) {
        let mut buffer = vec![0u16; size as usize];
        if unsafe { GetUserPreferredUILanguages(0x8, &mut count, buffer.as_mut_ptr(), &mut size) }
            != 0
        {
            let languages: Vec<String> = buffer
                .split(|c| *c == 0)
                .filter(|s| !s.is_empty())
                .map(String::from_utf16_lossy)
                .collect();
            if !languages.is_empty() {
                return languages;
            }
        }
    }
    let mut buffer = [0u16; 85];
    let length = unsafe { GetUserDefaultLocaleName(buffer.as_mut_ptr(), buffer.len() as i32) };
    if length > 1 && length as usize <= buffer.len() {
        return vec![String::from_utf16_lossy(&buffer[..length as usize - 1])];
    }
    Vec::new()
}
#[cfg(not(windows))]
pub fn system_languages() -> Vec<String> {
    env::var("LC_ALL")
        .ok()
        .or_else(|| env::var("LANG").ok())
        .filter(|v| !v.is_empty())
        .map(|v| vec![v.replace('_', "-")])
        .unwrap_or_default()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_codes_and_preserves_arguments() {
        assert!(safe_code("zh-Hant"));
        for code in ["../es", "es/../../en", "es.json", "", "en-"] {
            assert!(!safe_code(code));
        }
        let store = NativeLocaleStore::new(
            PathBuf::from("/missing-locales"),
            PathBuf::from("/missing-prefs"),
        );
        assert_eq!(
            store.text(
                "Open {name} at {path}",
                &[("name", "{path}".into()), ("path", "C:\\Photos".into())]
            ),
            "Open {path} at C:\\Photos"
        );
    }
    #[test]
    fn manifest_catalog_and_preferences_are_validated() {
        let root = env::temp_dir().join(format!(
            "lighttable-locale-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let languages: Vec<Value> = ["en", "es", "zh-Hans", "zh-Hant"]
            .iter()
            .map(|code| serde_json::json!({"code":code,"name":code,"nativeName":code,"dir":"ltr"}))
            .collect();
        fs::write(
            root.join("manifest.json"),
            serde_json::to_vec(
                &serde_json::json!({"version":1,"sourceLocale":"en","locales":languages}),
            )
            .unwrap(),
        )
        .unwrap();
        fs::write(root.join("es.json"), br#"{"version":1,"locale":"es","messages":{"Open {name}":"Abrir {name}","Delete {name}":"Borrar","Blank":" "}}"#).unwrap();
        fs::write(
            root.join("prefs.json"),
            br#"{"locale":"es","localeChosen":true,"other":42}"#,
        )
        .unwrap();
        let mut store = NativeLocaleStore::new(root.clone(), root.join("prefs.json"));
        assert_eq!(store.locale, "es");
        assert_eq!(store.suggested("es-MX"), "es");
        assert_eq!(store.suggested("zh_TW"), "zh-Hant");
        assert_eq!(store.suggested("zh-Hans-HK"), "zh-Hans");
        assert_eq!(store.suggested("fi-FI"), "en");
        assert_eq!(
            store.text("Open {name}", &[("name", "photo.jpg".into())]),
            "Abrir photo.jpg"
        );
        assert_eq!(
            store.text("Delete {name}", &[("name", "photo.jpg".into())]),
            "Delete photo.jpg"
        );
        assert_eq!(store.text("Blank", &[]), "Blank");
        fs::write(root.join("prefs.json"), b"broken").unwrap();
        assert!(store.reload().is_err());
        assert_eq!(store.locale, "es");
        assert_eq!(fs::read(root.join("prefs.json")).unwrap(), b"broken");
        store.set_locale("../en");
        assert_eq!(store.locale, "en");
        fs::remove_dir_all(root).unwrap();
    }
}
