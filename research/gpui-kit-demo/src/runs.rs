//! The runs in a runs folder, read from their manifests on disk (works without the engine).

use std::path::{Path, PathBuf};

use serde_json::Value;

#[derive(Clone, Debug, PartialEq)]
pub struct RunInfo {
    pub id: String,
    pub dir: PathBuf,
    pub state: String,
    pub source: String,
    pub created: String,
    /// The first key image, as a thumbnail, once the keys stage has run.
    pub thumb: Option<PathBuf>,
}

/// Newest first, like `c2a status`.
pub fn list(workdir: &Path) -> Vec<RunInfo> {
    let mut out: Vec<RunInfo> = std::fs::read_dir(workdir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| read(&e.path()))
        .collect();
    out.sort_by(|a, b| b.id.cmp(&a.id));
    out
}

/// A run folder's summary, or None if `dir` is not a run.
pub fn read(dir: &Path) -> Option<RunInfo> {
    let m: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("manifest.json")).ok()?).ok()?;
    let s = |k: &str| m.get(k).and_then(|x| x.as_str()).unwrap_or_default().to_string();
    let mut keys: Vec<PathBuf> = std::fs::read_dir(dir.join("keys"))
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "png" || x == "jpg"))
        .collect();
    keys.sort();
    let id = if s("id").is_empty() { dir.file_name()?.to_string_lossy().to_string() } else { s("id") };
    Some(RunInfo {
        created: when(&id),
        id,
        dir: dir.to_path_buf(),
        state: s("state"),
        source: display_name(&s("source")),
        thumb: keys.into_iter().next(),
    })
}

/// A source path as the user knows it: the file name, without the `<time>-<hex>-` prefix
/// the engine gives uploads.
pub fn display_name(source: &str) -> String {
    let name = Path::new(source).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    let parts: Vec<&str> = name.splitn(3, '-').collect();
    match parts[..] {
        [t, h, rest] if t.len() >= 9 && t.bytes().all(|b| b.is_ascii_digit()) && h.len() == 6
            && h.bytes().all(|b| b.is_ascii_hexdigit()) => rest.to_string(),
        _ => name,
    }
}

/// "2026-11-07 01:11" from a run id like 20261107-011121-3b76a5 (ids start with the time).
pub fn when(id: &str) -> String {
    let b = id.as_bytes();
    if b.len() >= 13 && b[8] == b'-' && b[..8].iter().chain(&b[9..13]).all(u8::is_ascii_digit) {
        format!("{}-{}-{} {}:{}", &id[0..4], &id[4..6], &id[6..8], &id[9..11], &id[11..13])
    } else {
        String::new()
    }
}

/// The newest exported video of a run (for read-only review without the engine).
pub fn latest_output(dir: &Path) -> Option<PathBuf> {
    std::fs::read_dir(dir.join("out"))
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "mp4"))
        .max_by_key(|p| std::fs::metadata(p).and_then(|m| m.modified()).ok())
}

pub fn is_video(p: &Path) -> bool {
    p.extension().is_some_and(|x| ["mp4", "mov", "mkv", "webm", "m4v"].contains(&x.to_string_lossy().to_lowercase().as_str()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_run_folders_newest_first_and_skips_others() {
        let w = std::env::temp_dir().join(format!("c2a-runs-{}", std::process::id()));
        for (id, state) in [("20261101-1", "done"), ("20261105-2", "preview")] {
            std::fs::create_dir_all(w.join(id).join("keys")).unwrap();
            std::fs::write(w.join(id).join("manifest.json"),
                format!(r#"{{"id":"{id}","state":"{state}","source":"/x/in/{id}.mp4","created":"t"}}"#)).unwrap();
        }
        std::fs::write(w.join("20261105-2/keys/img_000010.png"), b"").unwrap();
        std::fs::write(w.join("20261105-2/keys/img_000000.png"), b"").unwrap();
        std::fs::create_dir_all(w.join("uploads")).unwrap();
        let runs = list(&w);
        assert_eq!(runs.iter().map(|r| r.id.as_str()).collect::<Vec<_>>(), ["20261105-2", "20261101-1"]);
        assert_eq!(runs[0].source, "20261105-2.mp4");
        assert_eq!(runs[0].thumb.as_ref().unwrap().file_name().unwrap(), "img_000000.png");
        assert!(runs[1].thumb.is_none());
        assert!(is_video(Path::new("a.MOV")) && !is_video(Path::new("a.png")));
        assert_eq!(display_name("/w/uploads/1794013881-cd78fe-pan fry.mp4"), "pan fry.mp4");
        assert_eq!(display_name("/x/my-dish-2.mp4"), "my-dish-2.mp4");
        assert_eq!(when("20261107-011121-3b76a5"), "2026-11-07 01:11");
        assert_eq!(when("custom"), "");
        std::fs::remove_dir_all(&w).unwrap();
    }
}
