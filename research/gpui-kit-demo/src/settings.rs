//! What the app remembers between launches: ~/.config/c2a-review/settings.json (mode 0600,
//! it holds API keys), and the engine config it writes next to it (engine.yaml).

use std::path::PathBuf;

use serde_json::{Value, json};

use crate::engine::home;

/// Cloud AI (real models) or Demo (the engine's deterministic stand-ins: free, instant,
/// no AI — for trying the app).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub enum Mode {
    #[default]
    Cloud,
    Demo,
}

/// A vision LLM reachable with the OpenAI-compatible chat API.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct VlmPreset {
    pub name: &'static str,
    pub base_url: &'static str,
    pub model: &'static str,
    pub key_hint: &'static str,
}

pub const VLM_PRESETS: &[VlmPreset] = &[
    VlmPreset { name: "Gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai/",
                model: "gemini-2.5-flash", key_hint: "aistudio.google.com/apikey" },
    VlmPreset { name: "OpenAI", base_url: "https://api.openai.com/v1/", model: "gpt-4.1-mini",
                key_hint: "platform.openai.com/api-keys" },
    VlmPreset { name: "OpenRouter", base_url: "https://openrouter.ai/api/v1/",
                model: "qwen/qwen2.5-vl-72b-instruct", key_hint: "openrouter.ai/keys" },
    VlmPreset { name: "Qwen", base_url: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/",
                model: "qwen-vl-max", key_hint: "Alibaba Cloud Model Studio" },
    VlmPreset { name: "Ollama (local)", base_url: "http://127.0.0.1:11434/v1/", model: "qwen2.5vl:7b",
                key_hint: "no key needed" },
];

#[derive(Clone, Debug, PartialEq)]
pub struct Settings {
    /// The folder that holds the runs (`c2a studio --workdir`).
    pub workdir: Option<PathBuf>,
    /// The `c2a` executable, when it is not found automatically.
    pub c2a: Option<PathBuf>,
    /// Extra `c2a studio` arguments (advanced).
    pub engine_args: Vec<String>,
    pub mode: Mode,
    // vision model (OpenAI-compatible)
    pub vlm_base_url: String,
    pub vlm_model: String,
    pub vlm_key: String,
    // fal.ai
    pub fal_key: String,
    pub fal_video_resolution: String,
    pub fal_video_model: String,
    pub fal_image_model: String,
    // Fish Audio
    pub voice_enabled: bool,
    pub fish_key: String,
    pub voice_model: String,
    pub voice_reference: String,
    /// Set once the person has saved the settings screen at least once.
    pub configured: bool,
}

impl Default for Settings {
    fn default() -> Self {
        let g = VLM_PRESETS[0];
        Self {
            workdir: None, c2a: None, engine_args: Vec::new(), mode: Mode::Cloud,
            vlm_base_url: g.base_url.into(), vlm_model: g.model.into(), vlm_key: String::new(),
            fal_key: String::new(), fal_video_resolution: "480p".into(),
            fal_video_model: "fal-ai/wan/v2.2-a14b/image-to-video".into(),
            fal_image_model: "fal-ai/nano-banana/edit".into(),
            voice_enabled: true, fish_key: String::new(), voice_model: "drama-3".into(),
            voice_reference: String::new(), configured: false,
        }
    }
}

fn dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).or_else(|| home().map(|h| h.join(".config")))?;
    Some(base.join("c2a-review"))
}

impl Settings {
    pub fn load() -> Self {
        dir().and_then(|d| std::fs::read_to_string(d.join("settings.json")).ok())
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .map(|v| Self::from_json(&v))
            .unwrap_or_default()
    }

    pub fn save(&self) {
        let Some(d) = dir() else { return };
        let _ = std::fs::create_dir_all(&d);
        let f = d.join("settings.json");
        let _ = std::fs::write(&f, serde_json::to_string_pretty(&self.to_json()).unwrap_or_default());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o600));
        }
    }

    fn from_json(v: &Value) -> Self {
        let d = Self::default();
        let path = |k: &str| v.get(k).and_then(|x| x.as_str()).filter(|s| !s.is_empty()).map(PathBuf::from);
        let s = |k: &str, def: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or(def).to_string();
        let b = |k: &str, def: bool| v.get(k).and_then(|x| x.as_bool()).unwrap_or(def);
        Self {
            workdir: path("workdir"),
            c2a: path("c2a"),
            engine_args: v.get("engine_args").and_then(|a| a.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default(),
            mode: if v.get("mode").and_then(|m| m.as_str()) == Some("demo") { Mode::Demo } else { Mode::Cloud },
            vlm_base_url: s("vlm_base_url", &d.vlm_base_url),
            vlm_model: s("vlm_model", &d.vlm_model),
            vlm_key: s("vlm_key", ""),
            fal_key: s("fal_key", ""),
            fal_video_resolution: s("fal_video_resolution", &d.fal_video_resolution),
            fal_video_model: s("fal_video_model", &d.fal_video_model),
            fal_image_model: s("fal_image_model", &d.fal_image_model),
            voice_enabled: b("voice_enabled", true),
            fish_key: s("fish_key", ""),
            voice_model: s("voice_model", &d.voice_model),
            voice_reference: s("voice_reference", ""),
            configured: b("configured", false),
        }
    }

    fn to_json(&self) -> Value {
        json!({
            "workdir": self.workdir.as_ref().map(|p| p.to_string_lossy()),
            "c2a": self.c2a.as_ref().map(|p| p.to_string_lossy()),
            "engine_args": self.engine_args,
            "mode": if self.mode == Mode::Demo { "demo" } else { "cloud" },
            "vlm_base_url": self.vlm_base_url, "vlm_model": self.vlm_model, "vlm_key": self.vlm_key,
            "fal_key": self.fal_key, "fal_video_resolution": self.fal_video_resolution,
            "fal_video_model": self.fal_video_model, "fal_image_model": self.fal_image_model,
            "voice_enabled": self.voice_enabled, "fish_key": self.fish_key,
            "voice_model": self.voice_model, "voice_reference": self.voice_reference,
            "configured": self.configured,
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

    pub fn is_local_vlm(&self) -> bool {
        self.vlm_base_url.contains("127.0.0.1") || self.vlm_base_url.contains("localhost")
    }

    /// What is still missing before cloud runs can start (empty when ready).
    pub fn missing(&self) -> Vec<&'static str> {
        if self.mode == Mode::Demo {
            return Vec::new();
        }
        let mut v = Vec::new();
        if self.vlm_key.trim().is_empty() && !self.is_local_vlm() {
            v.push("a vision-model API key");
        }
        if self.fal_key.trim().is_empty() {
            v.push("a fal.ai API key");
        }
        if self.voice_enabled && self.fish_key.trim().is_empty() {
            v.push("a Fish Audio API key (or turn voice sounds off)");
        }
        v
    }

    /// The engine config (YAML; merged by the engine over its defaults) and the environment
    /// that carries the keys. Keys never go on the command line or into the YAML.
    pub fn engine_config(&self) -> (String, Vec<(String, String)>) {
        let q = |s: &str| serde_json::to_string(s).unwrap_or_default(); // JSON strings are YAML
        let stack = if self.mode == Mode::Demo { "fake" } else { "cloud" };
        let yaml = format!(
            "# written by c2a Review (Settings); edit there\n\
             run:\n  stack: {stack}\n\
             providers:\n  cloud_vlm:\n    base_url: {}\n    model: {}\n    api_key_env: C2A_VLM_KEY\n\
             \x20 fal:\n    api_key_env: FAL_KEY\n    video: {}\n    video_resolution: {}\n    image_edit: {}\n\
             \x20 fish:\n    api_key_env: FISH_API_KEY\n\
             audio:\n  voice:\n    enabled: {}\n    model: {}\n    reference_id: {}\n",
            q(&self.vlm_base_url), q(&self.vlm_model), q(&self.fal_video_model),
            q(&self.fal_video_resolution), q(&self.fal_image_model), self.voice_enabled,
            q(&self.voice_model),
            if self.voice_reference.trim().is_empty() { "null".into() } else { q(self.voice_reference.trim()) },
        );
        let vlm_key = if self.vlm_key.trim().is_empty() && self.is_local_vlm() { "local".to_string() } else { self.vlm_key.trim().to_string() };
        let env = vec![
            ("C2A_VLM_KEY".to_string(), vlm_key),
            ("FAL_KEY".to_string(), self.fal_key.trim().to_string()),
            ("FISH_API_KEY".to_string(), self.fish_key.trim().to_string()),
        ];
        (yaml, env)
    }

    /// Writes engine.yaml and returns its path.
    pub fn write_engine_config(&self) -> Option<PathBuf> {
        let d = dir()?;
        std::fs::create_dir_all(&d).ok()?;
        let f = d.join("engine.yaml");
        std::fs::write(&f, self.engine_config().0).ok()?;
        Some(f)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_through_json() {
        let s = Settings {
            workdir: Some("/data/runs".into()), mode: Mode::Demo, vlm_key: "k".into(),
            fal_key: "f".into(), voice_enabled: false, configured: true,
            engine_args: vec!["--set".into(), "output.fps=30".into()],
            ..Settings::default()
        };
        assert_eq!(Settings::from_json(&s.to_json()), s);
        assert_eq!(Settings::from_json(&json!({})), Settings::default());
    }

    #[test]
    fn says_what_is_missing() {
        let mut s = Settings::default();
        assert_eq!(s.missing().len(), 3);
        s.voice_enabled = false;
        s.fal_key = "f".into();
        assert_eq!(s.missing(), vec!["a vision-model API key"]);
        s.vlm_base_url = VLM_PRESETS[4].base_url.into(); // Ollama: no key
        assert!(s.missing().is_empty());
        s.mode = Mode::Demo;
        s.fal_key.clear();
        assert!(s.missing().is_empty());
    }

    #[test]
    fn engine_config_keeps_keys_out_of_the_yaml() {
        let s = Settings { vlm_key: "secret-v".into(), fal_key: "secret-f".into(), voice_reference: "abc".into(), ..Settings::default() };
        let (yaml, env) = s.engine_config();
        assert!(!yaml.contains("secret"));
        assert!(yaml.contains("stack: cloud") && yaml.contains("api_key_env: C2A_VLM_KEY"));
        assert!(yaml.contains("reference_id: \"abc\""));
        assert!(env.contains(&("FAL_KEY".into(), "secret-f".into())));
    }
}
