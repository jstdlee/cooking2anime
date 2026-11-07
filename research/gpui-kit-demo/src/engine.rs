//! Starts and stops the c2a engine (`c2a studio`) for the app, so nothing has to be run
//! by hand: a free local port, a random token, and the engine stops when the app does.

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use anyhow::{Context as _, Result, anyhow};

use crate::studio::Studio;

/// The engine process this app started (killed on quit, and by the kernel if the app dies).
static CHILD: Mutex<Option<Child>> = Mutex::new(None);

/// Where to look for the `c2a` executable, first match wins:
/// the setting or `--c2a`, `$C2A_BIN`, next to this app, `$PATH`, then `~/c2a-*/c2a`
/// (the folder the v0.2.0 release unpacks to).
pub fn find_c2a(configured: Option<&Path>) -> Option<PathBuf> {
    let mut cands: Vec<PathBuf> = Vec::new();
    cands.extend(configured.map(Path::to_path_buf));
    cands.extend(std::env::var_os("C2A_BIN").map(PathBuf::from));
    if let Some(dir) = std::env::current_exe().ok().and_then(|e| e.parent().map(Path::to_path_buf)) {
        cands.push(dir.join("c2a"));
        cands.extend(glob_dirs(dir.parent().unwrap_or(&dir), "c2a-").into_iter().map(|d| d.join("c2a")));
    }
    if let Some(path) = std::env::var_os("PATH") {
        cands.extend(std::env::split_paths(&path).map(|d| d.join("c2a")));
    }
    if let Some(home) = home() {
        cands.extend(glob_dirs(&home, "c2a-").into_iter().map(|d| d.join("c2a")));
    }
    cands.into_iter().find(|p| is_executable(p))
}

fn glob_dirs(dir: &Path, prefix: &str) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir() && p.file_name().is_some_and(|n| n.to_string_lossy().starts_with(prefix)))
        .collect();
    v.sort();
    v.reverse(); // newest version first
    v
}

fn is_executable(p: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        p.is_file() && p.metadata().map(|m| m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        p.is_file()
    }
}

pub fn home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

fn free_port() -> Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

fn random_token() -> String {
    use std::io::Read as _;
    let mut b = [0u8; 18];
    if std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut b)).is_err() {
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos();
        b[..16].copy_from_slice(&t.to_le_bytes());
    }
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// Starts `c2a studio` on `workdir` and waits until its API answers. Blocking: call it
/// off the UI thread. `extra` is added to the command line (e.g. `-c engine.yaml`); `env`
/// carries the API keys (never on the command line, where `ps` would show them).
pub fn start(c2a: &Path, workdir: &Path, extra: &[String], env: &[(String, String)]) -> Result<Studio> {
    stop();
    std::fs::create_dir_all(workdir).with_context(|| format!("cannot create {}", workdir.display()))?;
    let port = free_port()?;
    let token = random_token();
    let log = workdir.join(".c2a-review-engine.log");
    let mut cmd = Command::new(c2a);
    cmd.args(["studio", "--no-browser", "--host", "127.0.0.1", "--port", &port.to_string(), "--workdir"])
        .arg(workdir)
        .args(extra)
        .env("C2A_STUDIO_TOKEN", &token)
        .envs(env.iter().filter(|(_, v)| !v.is_empty()).map(|(k, v)| (k.as_str(), v.as_str())))
        // Relative config paths (configs/…) resolve from the project folder that holds `runs/`.
        .current_dir(workdir.parent().filter(|p| p.is_dir()).unwrap_or(workdir))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(std::fs::File::create(&log).map(Stdio::from).unwrap_or(Stdio::null()));
    #[cfg(target_os = "linux")]
    unsafe {
        use std::os::unix::process::CommandExt;
        // If this app dies without a clean quit, the kernel stops the engine too.
        cmd.pre_exec(|| {
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
            Ok(())
        });
    }
    let child = cmd.spawn().with_context(|| format!("cannot start {}", c2a.display()))?;
    *CHILD.lock().unwrap() = Some(child);

    let studio = Studio::new(&format!("http://127.0.0.1:{port}"), &token);
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        if studio.runs().is_ok() {
            return Ok(studio);
        }
        if let Some(code) = CHILD.lock().unwrap().as_mut().and_then(|c| c.try_wait().ok().flatten()) {
            let tail = std::fs::read_to_string(&log).unwrap_or_default();
            let tail: Vec<&str> = tail.lines().rev().take(6).collect::<Vec<_>>().into_iter().rev().collect();
            return Err(anyhow!("the engine exited ({code}): {}", tail.join(" | ")));
        }
        if Instant::now() > deadline {
            stop();
            return Err(anyhow!("the engine did not answer within 60 s (log: {})", log.display()));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// Stops the engine this app started, if any.
pub fn stop() {
    if let Some(mut c) = CHILD.lock().unwrap().take() {
        #[cfg(unix)]
        unsafe {
            libc::kill(c.id() as i32, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if c.try_wait().ok().flatten().is_some() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        let _ = c.kill();
        let _ = c.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_a_configured_executable_first() {
        let dir = std::env::temp_dir().join(format!("c2a-find-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let exe = dir.join("my-c2a");
        std::fs::write(&exe, "#!/bin/sh\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        assert_eq!(find_c2a(Some(&exe)), Some(exe.clone()));
        assert_ne!(find_c2a(Some(&dir.join("missing"))), Some(dir.join("missing")));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn tokens_are_random_hex() {
        let (a, b) = (random_token(), random_token());
        assert_eq!(a.len(), 36);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }
}
