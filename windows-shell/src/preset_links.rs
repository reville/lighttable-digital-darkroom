//! A narrow, local handoff channel for preset links received while the app runs.
//! Only catalog IDs cross this channel. No URL fetch or editing operation exists.
use anyhow::{Result, anyhow};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{Read, Write},
    net::{Ipv4Addr, Shutdown, SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

const PREFIX: &str = "lighttable://preset/";
const TIMEOUT: Duration = Duration::from_millis(500);

pub fn valid_preset_id(id: &str) -> bool {
    let parts: Vec<_> = id.split('/').collect();
    parts.len() == 2
        && parts.iter().all(|part| {
            (1..=64).contains(&part.len())
                && part
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        })
}

pub fn preset_id(raw: &str) -> Option<&str> {
    if raw.len() > PREFIX.len() + 129 {
        return None;
    }
    raw.strip_prefix(PREFIX).filter(|id| valid_preset_id(id))
}

#[derive(Serialize, Deserialize)]
struct Endpoint {
    port: u16,
    token: String,
}
#[derive(Serialize, Deserialize)]
struct Request {
    token: String,
    id: String,
}

fn endpoint_path(support: &Path) -> PathBuf {
    support.join("preset-link-endpoint.json")
}

/// False means no live receiver acknowledged the link; the caller can start up.
pub fn forward(support: &Path, id: &str) -> bool {
    if !valid_preset_id(id) {
        return false;
    }
    let attempt = || -> Result<bool> {
        let bytes = fs::File::open(endpoint_path(support))?
            .take(1024)
            .bytes()
            .collect::<std::io::Result<Vec<_>>>()?;
        let endpoint: Endpoint = serde_json::from_slice(&bytes)?;
        if endpoint.token.len() != 64 || !endpoint.token.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Ok(false);
        }
        let address = SocketAddr::from((Ipv4Addr::LOCALHOST, endpoint.port));
        let mut stream = TcpStream::connect_timeout(&address, TIMEOUT)?;
        stream.set_read_timeout(Some(TIMEOUT))?;
        stream.set_write_timeout(Some(TIMEOUT))?;
        stream.write_all(&serde_json::to_vec(&Request {
            token: endpoint.token,
            id: id.to_owned(),
        })?)?;
        stream.shutdown(Shutdown::Write)?;
        let mut ack = [0; 2];
        stream.read_exact(&mut ack)?;
        Ok(&ack == b"OK")
    };
    attempt().unwrap_or(false)
}

/// The listener belongs to the native app lifetime. Dropping it wakes and joins
/// the worker, then removes only its own discovery record.
pub struct PresetLinkInbox {
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    endpoint: Endpoint,
    path: PathBuf,
}
impl PresetLinkInbox {
    pub fn start(
        support: &Path,
        receive: impl Fn(String) -> bool + Send + 'static,
    ) -> Result<Self> {
        fs::create_dir_all(support)?;
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
        let mut entropy = [0u8; 32];
        getrandom::fill(&mut entropy)
            .map_err(|error| anyhow!("preset-link channel randomness failed: {error}"))?;
        let endpoint = Endpoint {
            port: listener.local_addr()?.port(),
            token: entropy.iter().map(|b| format!("{b:02x}")).collect(),
        };
        let path = endpoint_path(support);
        fs::write(&path, serde_json::to_vec(&endpoint)?)?;
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = stop.clone();
        let token = endpoint.token.clone();
        let worker = thread::spawn(move || {
            for connection in listener.incoming() {
                if worker_stop.load(Ordering::Acquire) {
                    break;
                }
                let Ok(mut stream) = connection else {
                    break;
                };
                let _ = stream.set_read_timeout(Some(TIMEOUT));
                let _ = stream.set_write_timeout(Some(TIMEOUT));
                let mut bytes = Vec::new();
                if (&mut stream).take(512).read_to_end(&mut bytes).is_err() {
                    continue;
                }
                let Ok(request) = serde_json::from_slice::<Request>(&bytes) else {
                    continue;
                };
                if request.token == token && valid_preset_id(&request.id) && receive(request.id) {
                    let _ = stream.write_all(b"OK");
                }
            }
        });
        Ok(Self {
            stop,
            worker: Some(worker),
            endpoint,
            path,
        })
    }
}
impl Drop for PresetLinkInbox {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let _ = TcpStream::connect_timeout(
            &SocketAddr::from((Ipv4Addr::LOCALHOST, self.endpoint.port)),
            TIMEOUT,
        );
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
        if fs::read(&self.path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Endpoint>(&bytes).ok())
            .is_some_and(|current| current.token == self.endpoint.token)
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accepts_only_bounded_canonical_catalog_ids() {
        for id in ["lighttable/warm-film", "alice-2/portrait-1"] {
            assert_eq!(preset_id(&format!("{PREFIX}{id}")), Some(id));
        }
        for suffix in [
            "",
            "a",
            "a/",
            "/a",
            "a/b/c",
            "../a",
            "a/../b",
            "a/%2e%2e",
            "a/b?url=https://evil.example",
            "a/b#x",
            "a/b\\c",
            "é/b",
            "A/b",
            "a/b\n",
            "a_b/c",
            "-a/b",
        ] {
            assert_eq!(preset_id(&format!("{PREFIX}{suffix}")), None, "{suffix:?}");
        }
        assert!(preset_id(&format!("{PREFIX}{}/{}", "a".repeat(64), "b".repeat(64))).is_some());
        assert!(preset_id(&format!("{PREFIX}a/{}", "b".repeat(65))).is_none());
        for raw in [
            "https://preset/a/b",
            "file:///a/b",
            "lighttable://preset:80/a/b",
            "lighttable://user@preset/a/b",
        ] {
            assert_eq!(preset_id(raw), None);
        }
    }
    #[test]
    fn handoff_repeats_and_stops_with_the_window() {
        let root =
            std::env::temp_dir().join(format!("lighttable-preset-links-{}", std::process::id()));
        let (tx, rx) = std::sync::mpsc::channel();
        assert!(!forward(&root, "lighttable/warm-film"));
        let inbox = PresetLinkInbox::start(&root, move |id| tx.send(id).is_ok()).unwrap();
        for _ in 0..2 {
            assert!(forward(&root, "lighttable/warm-film"));
            assert_eq!(rx.recv_timeout(TIMEOUT).unwrap(), "lighttable/warm-film");
        }
        assert!(!forward(&root, "../unsafe"));
        let mut wrong_endpoint: Endpoint =
            serde_json::from_slice(&fs::read(endpoint_path(&root)).unwrap()).unwrap();
        wrong_endpoint.token = "0".repeat(64);
        fs::write(
            endpoint_path(&root),
            serde_json::to_vec(&wrong_endpoint).unwrap(),
        )
        .unwrap();
        assert!(!forward(&root, "lighttable/warm-film"));
        fs::write(
            endpoint_path(&root),
            serde_json::to_vec(&inbox.endpoint).unwrap(),
        )
        .unwrap();
        drop(inbox);
        assert!(!endpoint_path(&root).exists());
        assert!(!forward(&root, "lighttable/warm-film"));
        fs::remove_dir_all(root).unwrap();
    }
}
