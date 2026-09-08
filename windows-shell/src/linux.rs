//! Linux desktop conventions, separate from the Windows and macOS hosts.
use anyhow::{Result, bail};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    path::{Path, PathBuf},
};

pub struct Directories {
    pub data: PathBuf,
    pub config: PathBuf,
    pub cache: PathBuf,
    pub state: PathBuf,
}

pub fn is_hyprland(get: impl Fn(&str) -> Option<OsString>) -> bool {
    get("HYPRLAND_INSTANCE_SIGNATURE").is_some_and(|value| !value.is_empty())
        || get("XDG_CURRENT_DESKTOP").is_some_and(|value| {
            value
                .to_string_lossy()
                .split(':')
                .any(|name| name.eq_ignore_ascii_case("Hyprland"))
        })
}

pub fn override_path(home: &Path, value: Option<OsString>) -> Option<PathBuf> {
    let value = value.filter(|value| !value.is_empty())?;
    let path = PathBuf::from(value);
    if let Ok(tail) = path.strip_prefix("~") {
        Some(home.join(tail))
    } else {
        Some(path)
    }
}

impl Directories {
    pub fn resolve(home: &Path, get: impl Fn(&str) -> Option<OsString>) -> Self {
        let base = |key, fallback| {
            get(key)
                .map(PathBuf::from)
                .filter(|path| path.is_absolute())
                .unwrap_or_else(|| home.join(fallback))
                .join("lighttable")
        };
        Self {
            data: base("XDG_DATA_HOME", ".local/share"),
            config: base("XDG_CONFIG_HOME", ".config"),
            cache: base("XDG_CACHE_HOME", ".cache"),
            state: base("XDG_STATE_HOME", ".local/state"),
        }
    }
}

pub fn validate_folder_name(raw: &str) -> Result<String> {
    // Linux names are case sensitive and may contain spaces, colons and dots.
    // Do not silently normalize a name the user chose.
    if raw.is_empty()
        || matches!(raw, "." | "..")
        || raw.contains('/')
        || raw.chars().any(char::is_control)
        || raw.len() > 255
    {
        bail!("Enter a valid folder name (up to 255 bytes, without /)")
    }
    Ok(raw.to_owned())
}

fn mount_path(encoded: &str) -> PathBuf {
    // mountinfo quotes whitespace and backslash as octal bytes. Decode once:
    // a literal backslash followed by 040 must not become a space.
    let bytes = encoded.as_bytes();
    let mut result = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'\\'
            && index + 3 < bytes.len()
            && bytes[index + 1..index + 4]
                .iter()
                .all(|v| (b'0'..=b'7').contains(v))
        {
            let value = ((bytes[index + 1] - b'0') as u16 * 64)
                + ((bytes[index + 2] - b'0') as u16 * 8)
                + (bytes[index + 3] - b'0') as u16;
            if value <= 255 {
                result.push(value as u8);
                index += 4;
                continue;
            }
        }
        result.push(bytes[index]);
        index += 1;
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;
        PathBuf::from(OsString::from_vec(result))
    }
    #[cfg(not(unix))]
    PathBuf::from(String::from_utf8_lossy(&result).into_owned())
}

/// Mounted media, including udisks' /run/media/USER/VOLUME convention.
pub fn media_mounts(mountinfo: &str) -> Vec<PathBuf> {
    mountinfo
        .lines()
        .filter_map(|line| line.split_whitespace().nth(4))
        .map(mount_path)
        .filter(|path| {
            ["/media", "/run/media", "/mnt"]
                .iter()
                .any(|base| path.starts_with(base) && path != Path::new(base))
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn xdg_defaults_and_absolute_overrides_agree_with_python() {
        let dirs = Directories::resolve(Path::new("/home/test"), |key| match key {
            "XDG_DATA_HOME" => Some("/media/config/data".into()),
            "XDG_CONFIG_HOME" => Some("relative/is/invalid".into()),
            "XDG_CACHE_HOME" => Some("".into()),
            _ => None,
        });
        assert_eq!(dirs.data, Path::new("/media/config/data/lighttable"));
        assert_eq!(dirs.config, Path::new("/home/test/.config/lighttable"));
        assert_eq!(dirs.cache, Path::new("/home/test/.cache/lighttable"));
        assert_eq!(dirs.state, Path::new("/home/test/.local/state/lighttable"));
    }

    #[test]
    fn linux_names_preserve_case_whitespace_and_valid_punctuation() {
        for name in ["Trip: 2026", "CON", ".archive", "two  spaces", "trailing."] {
            assert_eq!(validate_folder_name(name).unwrap(), name);
        }
        for name in ["", ".", "..", "../elsewhere", "line\nbreak", "nul\0"] {
            assert!(validate_folder_name(name).is_err());
        }
        assert!(validate_folder_name(&"é".repeat(128)).is_err());
    }

    #[test]
    fn application_overrides_expand_home_and_ignore_empty_values() {
        let home = Path::new("/home/test");
        assert_eq!(
            override_path(home, Some("~/photos/cache".into())),
            Some(home.join("photos/cache"))
        );
        assert_eq!(
            override_path(home, Some("/custom/cache".into())),
            Some(PathBuf::from("/custom/cache"))
        );
        assert_eq!(override_path(home, Some("".into())), None);
    }

    #[test]
    fn compositor_detection_accepts_desktop_lists_without_guessing_other_desktops() {
        assert!(is_hyprland(
            |key| (key == "XDG_CURRENT_DESKTOP").then(|| "Omarchy:Hyprland".into())
        ));
        assert!(is_hyprland(
            |key| (key == "HYPRLAND_INSTANCE_SIGNATURE").then(|| "session".into())
        ));
        assert!(!is_hyprland(
            |key| (key == "XDG_CURRENT_DESKTOP").then(|| "GNOME".into())
        ));
        assert!(!is_hyprland(|_| Some("".into())));
    }

    #[test]
    fn mounted_media_are_decoded_and_deduplicated() {
        let entries = "1 0 8:0 / / rw - ext4 /dev/a rw\n2 1 8:1 / /run/media/test/My\\040Card rw - vfat /dev/b rw\n3 1 8:2 / /media/test/Photos rw - ext4 /dev/c rw\n4 1 8:1 / /run/media/test/My\\040Card rw - vfat /dev/b rw\n5 1 8:3 / /mnt rw - ext4 /dev/d rw\n";
        assert_eq!(
            media_mounts(entries),
            vec![
                PathBuf::from("/media/test/Photos"),
                PathBuf::from("/run/media/test/My Card")
            ]
        );
        assert_eq!(
            mount_path(r"/mnt/literal\134040"),
            PathBuf::from(r"/mnt/literal\040")
        );
    }
}
