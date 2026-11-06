//! Client for the c2a Studio API (`c2a studio`): the run view and AI operations.
//!
//! The demo does not reimplement any engine logic: it reads the same view model the web
//! Studio draws (`GET /api/runs/{id}`) and sends the same typed commands
//! (`POST /api/runs/{id}/commands`), which call `c2a.app` in the engine.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context as _, Result, anyhow};
use serde_json::{Value, json};

#[derive(Clone)]
pub struct Studio {
    base: String,
    token: String,
    agent: ureq::Agent,
}

impl Studio {
    pub fn new(base: &str, token: &str) -> Self {
        let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(10)).build();
        Self { base: base.trim_end_matches('/').to_string(), token: token.to_string(), agent }
    }

    pub fn view(&self, run: &str) -> Result<View> {
        let v: Value = self
            .agent
            .get(&format!("{}/api/runs/{run}", self.base))
            .set("X-C2A-Token", &self.token)
            .call()
            .map_err(http_error)?
            .into_json()
            .context("view is not JSON")?;
        View::from_json(&v)
    }

    /// Sends one engine command (the same ones the web Studio and the CLI use).
    pub fn command(&self, run: &str, op: &str, args: Value) -> Result<()> {
        self.agent
            .post(&format!("{}/api/runs/{run}/commands", self.base))
            .set("X-C2A-Token", &self.token)
            .send_json(json!({ "op": op, "args": args }))
            .map_err(http_error)?;
        Ok(())
    }
}

fn http_error(e: ureq::Error) -> anyhow::Error {
    match e {
        ureq::Error::Status(code, resp) => {
            let detail = resp
                .into_json::<Value>()
                .ok()
                .and_then(|v| v.get("detail").and_then(|d| d.as_str()).map(String::from))
                .unwrap_or_default();
            anyhow!("Studio refused ({code}): {detail}")
        }
        other => anyhow!("Studio unreachable: {other}"),
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Key {
    pub start: u64,
    pub t: f64,
    pub checked: bool,
    pub locked: bool,
    pub thumb: Option<String>,
    pub action: String,
    pub motion: String,
    pub camera: String,
    pub importance: f64,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Sample {
    pub id: String,
    pub image: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Clip {
    pub id: String,
    pub src: (f64, f64),
    pub out: (f64, f64),
    pub clip_s: f64,
    pub status: String,
    pub video: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct View {
    pub run: String,
    pub state: String,
    pub busy: bool,
    pub note: Option<String>,
    pub keys: Vec<Key>,
    pub samples: Vec<Sample>,
    pub samples_current: bool,
    pub approved: Option<String>,
    pub clips: Vec<Clip>,
    pub output: Option<String>,
    pub director: Option<String>,
    pub audio_mode: String,
    pub directors: Vec<(String, String)>,
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or_default().to_string()
}

fn f(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or_default()
}

fn pair(v: &Value, k: &str) -> (f64, f64) {
    let a = v.get(k).and_then(|x| x.as_array());
    let g = |i: usize| a.and_then(|a| a.get(i)).and_then(|x| x.as_f64()).unwrap_or_default();
    (g(0), g(1))
}

impl View {
    pub fn from_json(v: &Value) -> Result<Self> {
        let run = s(v, "run");
        if run.is_empty() {
            return Err(anyhow!("not a run view"));
        }
        let keys = v
            .get("keys")
            .and_then(|k| k.as_array())
            .map(|ks| {
                ks.iter()
                    .map(|k| {
                        let tags = k.get("tags").cloned().unwrap_or(Value::Null);
                        let motion = tags.get("motion").cloned().unwrap_or(Value::Null);
                        Key {
                            start: k.get("start").and_then(|x| x.as_u64()).unwrap_or_default(),
                            t: f(k, "t"),
                            checked: k.get("checked").and_then(|x| x.as_bool()).unwrap_or(false),
                            locked: k.get("locked").and_then(|x| x.as_bool()).unwrap_or(false),
                            thumb: k.get("thumb").and_then(|x| x.as_str()).map(String::from),
                            action: s(&tags, "action").replace('_', " "),
                            motion: format!("{} {}", s(&motion, "direction"), s(&motion, "speed"))
                                .replace('_', " ")
                                .trim()
                                .to_string(),
                            camera: s(&tags, "camera").replace('_', " "),
                            importance: f(&tags, "importance"),
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        let style = v.get("style").cloned().unwrap_or(Value::Null);
        let samples = style
            .get("samples")
            .and_then(|x| x.as_array())
            .map(|ss| ss.iter().map(|x| Sample { id: s(x, "id"), image: s(x, "image") }).collect())
            .unwrap_or_default();
        let approval = style.get("approval").cloned().unwrap_or(Value::Null);
        let approved = approval
            .get("current")
            .and_then(|c| c.as_bool())
            .filter(|c| *c)
            .map(|_| s(&approval, "candidate"));
        let clip_status: Vec<(String, String, Option<String>)> = v
            .get("clips")
            .and_then(|x| x.as_array())
            .map(|cs| {
                cs.iter()
                    .map(|c| (s(c, "id"), s(c, "status"), c.get("video").and_then(|x| x.as_str()).map(String::from)))
                    .collect()
            })
            .unwrap_or_default();
        let clips = v
            .get("plan")
            .and_then(|p| p.get("clips"))
            .and_then(|x| x.as_array())
            .map(|cs| {
                cs.iter()
                    .map(|c| {
                        let id = s(c, "id");
                        let st = clip_status.iter().find(|x| x.0 == id);
                        Clip {
                            src: pair(c, "src"),
                            out: pair(c, "out"),
                            clip_s: f(c, "clip_s"),
                            status: st.map(|x| x.1.clone()).unwrap_or_else(|| "pending".into()),
                            video: st.and_then(|x| x.2.clone()),
                            id,
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        let settings = v.get("settings").cloned().unwrap_or(Value::Null);
        let directors = v
            .get("catalog")
            .and_then(|c| c.get("director"))
            .and_then(|d| d.as_array())
            .map(|ds| ds.iter().map(|d| (s(d, "name"), s(d, "title"))).collect())
            .unwrap_or_default();
        Ok(View {
            run,
            state: s(v, "state"),
            busy: v.get("busy").and_then(|b| b.as_bool()).unwrap_or(false),
            note: v.get("note").and_then(|n| n.as_str()).map(String::from),
            keys,
            samples,
            samples_current: style.get("samples_current").and_then(|x| x.as_bool()).unwrap_or(false),
            approved,
            clips,
            output: v.get("output").and_then(|o| o.get("video")).and_then(|x| x.as_str()).map(String::from),
            director: settings
                .get("style")
                .and_then(|st| st.get("director"))
                .and_then(|d| d.as_str())
                .map(String::from),
            audio_mode: settings
                .get("audio")
                .and_then(|a| a.get("mode"))
                .and_then(|m| m.as_str())
                .unwrap_or("regenerate")
                .to_string(),
            directors,
        })
    }

    /// The source time shown at output time `t` (clips may be shorter or longer than the
    /// source span they come from, so time is mapped linearly inside each clip).
    pub fn source_time(&self, t: f64) -> f64 {
        for c in &self.clips {
            if t >= c.out.0 && t < c.out.1 && c.out.1 > c.out.0 {
                return c.src.0 + (t - c.out.0) / (c.out.1 - c.out.0) * (c.src.1 - c.src.0);
            }
        }
        self.clips.last().map(|c| c.src.1).unwrap_or(t)
    }
}

/// Files of a run on this machine (the demo reads media locally, the view over HTTP).
pub fn media_path(run_dir: &Path, rel: &str) -> PathBuf {
    run_dir.join(rel)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Value {
        json!({
            "run": "r1", "state": "done", "busy": false,
            "keys": [{"start": 0, "t": 0.0, "checked": true, "locked": true, "thumb": "keys/img_000000.png",
                      "tags": {"action": "pat_dry", "motion": {"direction": "toward_camera", "speed": "fast"},
                               "camera": "push_in", "importance": 0.8}}],
            "style": {"samples": [{"id": "0:1", "image": "preview/s0_k1.png", "key": 0}],
                      "samples_current": true, "approval": {"candidate": "0:1", "current": true}},
            "plan": {"clips": [{"id": "k000000", "src": [0.0, 4.0], "out": [0.0, 2.0], "clip_s": 2.0},
                               {"id": "k000120", "src": [4.0, 6.0], "out": [2.0, 4.0], "clip_s": 2.0}]},
            "clips": [{"id": "k000000", "status": "done", "video": "generate/k000000.mp4"}],
            "output": {"video": "out/x_720p.mp4"},
            "settings": {"style": {"director": "ozu_inspired"}, "audio": {"mode": "silent"}},
            "catalog": {"director": [{"name": "ozu_inspired", "title": "Ozu-inspired"}]}
        })
    }

    #[test]
    fn parses_the_engine_view() {
        let v = View::from_json(&sample()).unwrap();
        assert_eq!(v.keys[0].action, "pat dry");
        assert_eq!(v.keys[0].motion, "toward camera fast");
        assert_eq!(v.approved.as_deref(), Some("0:1"));
        assert_eq!(v.clips[0].status, "done");
        assert_eq!(v.clips[1].status, "pending");
        assert_eq!(v.output.as_deref(), Some("out/x_720p.mp4"));
        assert_eq!(v.director.as_deref(), Some("ozu_inspired"));
        assert_eq!(v.directors, vec![("ozu_inspired".into(), "Ozu-inspired".into())]);
        assert!(View::from_json(&json!({})).is_err());
    }

    #[test]
    fn output_time_maps_to_source_time_per_clip() {
        let v = View::from_json(&sample()).unwrap();
        assert_eq!(v.source_time(0.0), 0.0);
        assert_eq!(v.source_time(1.0), 2.0); // clip 1 plays 4 s of source in 2 s
        assert_eq!(v.source_time(3.0), 5.0);
        assert_eq!(v.source_time(9.0), 6.0); // past the end: the last source time
    }
}
