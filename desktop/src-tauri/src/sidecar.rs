//! The trusted production backend, owned by the desktop app (Phase 17).
//!
//! A release build of Sam.app starts ITS OWN backend and nothing else:
//!
//! * the executable is fixed: `Contents/Resources/backend/python/bin/python3.12`
//!   inside this app bundle, resolved from the running executable and
//!   canonicalized; anything outside the bundle is refused (no repository,
//!   no project `.venv`, no system Python, no `PATH` lookup);
//! * argv only (`std::process::Command`, no shell): `-I -B -m
//!   sam.system.backend`. `-I` is Python's isolated mode: no user site, no
//!   `PYTHON*` variables, no working directory on `sys.path`;
//! * a cleared environment with exactly: production mode, a per-launch
//!   random bridge token (never shown to the webview), a free loopback port,
//!   `HOME`/`TMPDIR` (for the owner's data directory and the Keychain) and a
//!   minimal `PATH`. No provider credential is ever passed: in production the
//!   backend reads them from the Keychain only;
//! * the backend binds 127.0.0.1 only; readiness is a finite wait on its
//!   public `/health`; if it does not become ready the shell stays up with no
//!   backend (every bridge call is `not_configured`: the UI shows Sam as
//!   unavailable) and the process is killed;
//! * shutdown: the child's stdin is closed (it exits on EOF, and would even if
//!   this app crashed), with a bounded wait and a kill as the last resort;
//! * duplicate instances: the backend's own instance lock refuses a second
//!   Sam for the same data directory (it then serves only a BLOCKED status).

use std::io::Read;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::time::{Duration, Instant};

use crate::backend::BACKEND_HOST;

pub const BACKEND_MODULE: &str = "sam.system.backend";
pub const READY_TIMEOUT: Duration = Duration::from_secs(45);
pub const STOP_TIMEOUT: Duration = Duration::from_secs(10);
const MINIMAL_PATH: &str = "/usr/bin:/bin:/usr/sbin:/sbin";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SidecarError {
    NotBundled,
    Unsafe,
    Spawn,
    NotReady,
}

impl SidecarError {
    pub fn as_code(self) -> &'static str {
        match self {
            SidecarError::NotBundled => "backend_not_bundled",
            SidecarError::Unsafe => "backend_path_unsafe",
            SidecarError::Spawn => "backend_spawn_failed",
            SidecarError::NotReady => "backend_not_ready",
        }
    }
}

/// `Sam.app/Contents/MacOS/<exe>` -> `Sam.app/Contents/Resources/backend/...`
pub fn bundled_python(exe: &Path) -> Result<PathBuf, SidecarError> {
    let exe = exe.canonicalize().map_err(|_| SidecarError::NotBundled)?;
    let contents = exe
        .parent()
        .and_then(Path::parent)
        .ok_or(SidecarError::NotBundled)?
        .to_path_buf();
    let candidate = contents.join("Resources/backend/python/bin/python3.12");
    let python = candidate
        .canonicalize()
        .map_err(|_| SidecarError::NotBundled)?;
    if !python.starts_with(&contents) || !python.is_file() {
        return Err(SidecarError::Unsafe);
    }
    Ok(python)
}

fn random_token() -> Result<String, SidecarError> {
    let mut bytes = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| SidecarError::Spawn)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn free_loopback_port() -> Result<u16, SidecarError> {
    let listener = TcpListener::bind((BACKEND_HOST, 0)).map_err(|_| SidecarError::Spawn)?;
    let port = listener
        .local_addr()
        .map_err(|_| SidecarError::Spawn)?
        .port();
    Ok(port)
}

/// The login account name, only if it is a plain account name. The backend's
/// Claude subscription provider needs it: the Claude CLI finds the owner's own
/// login (in the macOS Keychain) by account name. Anything unusual is dropped.
fn account_name() -> Option<String> {
    let value = std::env::var("USER").ok()?;
    let plain = !value.is_empty()
        && value.len() <= 64
        && !value.starts_with('-')
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'));
    plain.then_some(value)
}

fn absolute_env(name: &str, fallback: &str) -> String {
    match std::env::var(name) {
        Ok(value) if value.starts_with('/') => value,
        _ => fallback.to_string(),
    }
}

pub struct Sidecar {
    child: Child,
    stdin: Option<ChildStdin>,
    pub port: u16,
    pub token: String,
}

impl Sidecar {
    /// Start the bundled backend and wait until it answers `/health`.
    /// `data_dir` is only for the headless self-test (a temporary directory);
    /// a normal launch uses the owner's application-data directory.
    pub fn start(python: &Path, data_dir: Option<&Path>) -> Result<Sidecar, SidecarError> {
        let token = random_token()?;
        let port = free_loopback_port()?;
        let backend_dir = python
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .ok_or(SidecarError::Unsafe)?;
        let mut command = Command::new(python);
        command
            .args(["-I", "-B", "-m", BACKEND_MODULE])
            .env_clear()
            .env("APP_ENV", "production")
            .env("LOG_LEVEL", "INFO")
            .env("API_PORT", port.to_string())
            .env("DESKTOP_BRIDGE_TOKEN", &token)
            .env("HOME", absolute_env("HOME", "/var/empty"))
            .env("TMPDIR", absolute_env("TMPDIR", "/tmp"))
            .env("PATH", MINIMAL_PATH)
            .env("LANG", "en_US.UTF-8")
            // Voice is Sam's primary interaction. This only allows the local
            // voice stack: it stays off unless the owner has explicitly set up
            // the verified local models (sam.voice_local.setup).
            .env("VOICE_IDENTITY_ENABLED", "true")
            .current_dir(backend_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        if let Some(dir) = data_dir {
            command.env("SAM_DATA_DIR", dir);
        }
        if let Some(account) = account_name() {
            command.env("USER", &account).env("LOGNAME", &account);
        }
        let mut child = command.spawn().map_err(|_| SidecarError::Spawn)?;
        let stdin = child.stdin.take();
        let mut sidecar = Sidecar {
            child,
            stdin,
            port,
            token,
        };
        if sidecar.wait_ready(READY_TIMEOUT) {
            Ok(sidecar)
        } else {
            sidecar.stop();
            Err(SidecarError::NotReady)
        }
    }

    fn wait_ready(&mut self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .max_redirects(0)
            .proxy(None)
            .timeout_global(Some(Duration::from_secs(2)))
            .build()
            .into();
        let url = format!("http://{}:{}/health", BACKEND_HOST, self.port);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = self.child.try_wait() {
                return false; // exited early (e.g. refused to start)
            }
            if let Ok(response) = agent.get(&url).call() {
                if response.status().as_u16() == 200 {
                    return true;
                }
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        false
    }

    /// Close stdin (the backend exits on EOF), wait, then kill if needed.
    /// Returns true when the backend exited on its own.
    pub fn stop(&mut self) -> bool {
        drop(self.stdin.take());
        let deadline = Instant::now() + STOP_TIMEOUT;
        while Instant::now() < deadline {
            if let Ok(Some(_)) = self.child.try_wait() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
        false
    }

    pub fn has_exited(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(Some(_)))
    }
}

/// Start the backend bundled with THIS executable (a normal release launch).
pub fn start_owned() -> Result<Sidecar, SidecarError> {
    let exe = std::env::current_exe().map_err(|_| SidecarError::NotBundled)?;
    let python = bundled_python(&exe)?;
    Sidecar::start(&python, None)
}

/// Headless check of a built artifact: start the bundled backend with a
/// temporary data directory, authenticate over the bridge, read status and
/// the owner's grants, stop it, and print one line of JSON metadata.
pub fn self_test(data_dir: &str) -> i32 {
    let dir = Path::new(data_dir);
    if !dir.is_absolute() {
        println!("{{\"self_test\":\"failed\",\"reason\":\"data_dir_not_absolute\"}}");
        return 2;
    }
    let exe = match std::env::current_exe() {
        Ok(path) => path,
        Err(_) => return 2,
    };
    let python = match bundled_python(&exe) {
        Ok(path) => path,
        Err(error) => {
            println!(
                "{{\"self_test\":\"failed\",\"reason\":\"{}\"}}",
                error.as_code()
            );
            return 1;
        }
    };
    let mut sidecar = match Sidecar::start(&python, Some(dir)) {
        Ok(sidecar) => sidecar,
        Err(error) => {
            println!(
                "{{\"self_test\":\"failed\",\"reason\":\"{}\"}}",
                error.as_code()
            );
            return 1;
        }
    };
    let backend = crate::backend::Backend::new(sidecar.port, Some(sidecar.token.clone()));
    let status = backend.call(crate::backend::Route::Status, None);
    let grants = backend.call(crate::backend::Route::Permissions, None);
    let unauthenticated = crate::backend::Backend::new(sidecar.port, Some("x".repeat(40)))
        .call(crate::backend::Route::Status, None);
    let clean = sidecar.stop();
    let exited = sidecar.has_exited();
    let health = status
        .as_ref()
        .ok()
        .and_then(|v| v.get("health"))
        .and_then(|h| h.get("status"))
        .and_then(|s| s.as_str())
        .unwrap_or("none")
        .to_string();
    let storage = status
        .as_ref()
        .ok()
        .and_then(|v| v.get("health"))
        .and_then(|h| h.get("storage_mode"))
        .and_then(|s| s.as_str())
        .unwrap_or("none")
        .to_string();
    let grant_count = grants
        .as_ref()
        .ok()
        .and_then(|v| v.get("grants"))
        .and_then(|g| g.as_array())
        .map_or(0, Vec::len);
    let ok = status.is_ok()
        && grants.is_ok()
        && unauthenticated.is_err()
        && exited
        && storage == "sqlite";
    println!(
        "{{\"self_test\":\"{}\",\"bridge\":\"{}\",\"health\":\"{}\",\"storage\":\"{}\",\
         \"grants\":{},\"wrong_token_refused\":{},\"shutdown\":\"{}\",\"exited\":{}}}",
        if ok { "ok" } else { "failed" },
        if status.is_ok() {
            "authenticated"
        } else {
            "failed"
        },
        health,
        storage,
        grant_count,
        unauthenticated.is_err(),
        if clean { "clean" } else { "killed" },
        exited
    );
    if ok {
        0
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn only_a_plain_account_name_is_passed_on() {
        let saved = std::env::var("USER").ok();
        for (value, ok) in [
            ("owner", true),
            ("first.last_1-x", true),
            ("", false),
            ("-rf", false),
            ("a b", false),
            ("x;y", false),
            ("../x", false),
        ] {
            std::env::set_var("USER", value);
            assert_eq!(account_name().is_some(), ok, "{value:?}");
        }
        match saved {
            Some(v) => std::env::set_var("USER", v),
            None => std::env::remove_var("USER"),
        }
    }

    use super::*;
    use std::fs;
    use std::os::unix::fs::symlink;

    fn fake_bundle(root: &Path) -> PathBuf {
        let macos = root.join("Sam.app/Contents/MacOS");
        fs::create_dir_all(&macos).unwrap();
        let exe = macos.join("sam-desktop");
        fs::write(&exe, b"").unwrap();
        exe
    }

    fn unique(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("sam-sidecar-{name}-{}", random_token().unwrap()));
        fs::create_dir_all(&dir).unwrap();
        dir.canonicalize().unwrap()
    }

    #[test]
    fn the_python_must_be_the_one_inside_this_bundle() {
        let root = unique("ok");
        let exe = fake_bundle(&root);
        assert_eq!(bundled_python(&exe), Err(SidecarError::NotBundled));
        let bin = root.join("Sam.app/Contents/Resources/backend/python/bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("python3.12"), b"").unwrap();
        let found = bundled_python(&exe).unwrap();
        assert_eq!(found, bin.join("python3.12"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_python_linked_from_outside_the_bundle_is_refused() {
        let root = unique("link");
        let exe = fake_bundle(&root);
        let outside = root.join("dev-venv-python3.12");
        fs::write(&outside, b"").unwrap();
        let bin = root.join("Sam.app/Contents/Resources/backend/python/bin");
        fs::create_dir_all(&bin).unwrap();
        symlink(&outside, bin.join("python3.12")).unwrap();
        assert_eq!(bundled_python(&exe), Err(SidecarError::Unsafe));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn tokens_are_random_and_long_enough_for_the_bridge() {
        let a = random_token().unwrap();
        let b = random_token().unwrap();
        assert_ne!(a, b);
        assert_eq!(a.len(), 64);
        assert!(crate::backend::valid_token(Some(&a)).is_some());
    }
}
