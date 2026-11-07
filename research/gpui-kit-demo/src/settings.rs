//! What the app remembers between launches: ~/.config/c2a-review/settings.json.

use std::path::PathBuf;

use serde_json::{Value, json};

use crate::engine::home;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Settings {
    /// The folder that holds the runs (`c2a studio --workdir`).
    pub workdir: Option<PathBuf>,
    /// The `c2a` executable, when it is not found automatically.
    pub c2a: Option<PathBuf>,
    /// Extra `c2a studio` arguments, e.g. ["--stack", "cloud"] or ["-c", "my.yaml"].
    pub engine_args: Vec<String>,
}

fn file() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).or_else(|| home().map(|h| h.join(".config")))?;
    Some(base.join("c2a-review").join("settings.json"))
}

impl Settings {
    pub fn load() -> Self {
        file().and_then(|f| std::fs::read_to_string(f).ok()).and_then(|s| serde_json::from_str::<Value>(&s).ok()).map(|v| Self::from_json(&v)).unwrap_or_default()
    }

    pub fn save(&self) {
        if let Some(f) = file() {
            let _ = std::fs::create_dir_all(f.parent().unwrap());
            let _ = std::fs::write(f, serde_json::to_string_pretty(&self.to_json()).unwrap_or_default());
        }
    }

    fn from_json(v: &Value) -> Self {
        let path = |k: &str| v.get(k).and_then(|x| x.as_str()).filter(|s| !s.is_empty()).map(PathBuf::from);
        Self {
            workdir: path("workdir"),
            c2a: path("c2a"),
            engine_args: v
                .get("engine_args")
                .and_then(|a| a.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                .unwrap_or_default(),
        }
    }

    fn to_json(&self) -> Value {
        json!({
            "workdir": self.workdir.as_ref().map(|p| p.to_string_lossy()),
            "c2a": self.c2a.as_ref().map(|p| p.to_string_lossy()),
            "engine_args": self.engine_args,
        })
    }

    /// The runs folder to use: the setting, else `./runs` if it exists, else `~/c2a/runs`.
    pub fn workdir_or_default(&self) -> PathBuf {
        if let Some(w) = &self.workdir {
            return w.clone();
        }
        let local = PathBuf::from("runs");
        if local.join(".").is_dir() {
            return std::fs::canonicalize(&local).unwrap_or(local);
        }
        home().map(|h| h.join("c2a").join("runs")).unwrap_or(local)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_through_json() {
        let s = Settings { workdir: Some("/data/runs".into()), c2a: None, engine_args: vec!["--stack".into(), "cloud".into()] };
        assert_eq!(Settings::from_json(&s.to_json()), s);
        assert_eq!(Settings::from_json(&json!({})), Settings::default());
    }
}
