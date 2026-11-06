//! c2a Review: a native desktop demo on GPUI Kit (research branch).
//!
//!   c2a-review --run-dir runs/<id> --studio http://127.0.0.1:8765 --token <token>
//!
//! - Review: source and output video side by side with one transport (play/pause,
//!   scrub on the output timeline, the source follows the clip mapping) and a clip list.
//! - Canvas: key images and style samples on a pannable, zoomable surface (drag, wheel).
//! - AI operations: check/uncheck key images, approve a sample, re-run a clip, switch the
//!   director preset or sound mode. Each is an engine command sent to `c2a studio`.

mod studio;
mod video;

use std::path::{Path, PathBuf};
use std::time::Duration;

use gpui_kit::component::button::*;
use gpui_kit::component::checkbox::Checkbox;
use gpui_kit::component::slider::{Slider, SliderEvent, SliderState};
use gpui_kit::component::{h_flex, v_flex, ActiveTheme as _, Disableable as _, Selectable as _, Sizable as _};
use gpui_kit::*;
use serde_json::json;

use studio::{Studio, View, media_path};
use video::{PlayerConfig, Source, VideoPlayer, video_view};

const POLL: Duration = Duration::from_millis(1500);
const SYNC: Duration = Duration::from_millis(250);
const CARD: f32 = 220.0; // canvas card width in world units (16:9 image + caption)

#[derive(Clone, Debug, PartialEq)]
enum Selected {
    Key(u64),
    Sample(String),
}

struct Canvas {
    zoom: f32,
    pan: Point<Pixels>,
    drag_from: Option<Point<Pixels>>,
    selected: Option<Selected>,
}

struct ReviewApp {
    run_dir: PathBuf,
    run_id: String,
    studio: Option<Studio>,
    view: Option<View>,
    status: String,
    source: Entity<VideoPlayer>,
    output: Option<(String, Entity<VideoPlayer>)>,
    scrub: Entity<SliderState>,
    scrubbing: bool,
    canvas: Canvas,
    _tasks: Vec<Task<()>>,
}

fn open_player(path: &Path, cx: &mut App) -> Entity<VideoPlayer> {
    let src = Source::parse(&path.to_string_lossy()).expect("valid path");
    let config = PlayerConfig { autoplay: false, ..Default::default() };
    cx.new(|cx| VideoPlayer::new(src, config, cx))
}

impl ReviewApp {
    fn new(run_dir: PathBuf, studio: Option<Studio>, window: &mut Window, cx: &mut Context<Self>) -> Self {
        let run_id = run_dir.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        let source = open_player(&run_dir.join("ingest/normalized.mp4"), cx);
        cx.observe(&source, |_, _, cx| cx.notify()).detach();
        let scrub = cx.new(|_| SliderState::new().min(0.0).max(1.0).step(0.001));
        cx.subscribe_in(&scrub, window, |this: &mut Self, _, ev: &SliderEvent, _, cx| {
            let (SliderEvent::Change(v) | SliderEvent::Release(v)) = ev;
            this.scrubbing = matches!(ev, SliderEvent::Change(_));
            this.seek_fraction(v.start(), cx);
        })
        .detach();

        let mut tasks = Vec::new();
        // Poll the engine's view (state, keys, samples, clips) while the window is open.
        tasks.push(cx.spawn(async move |this, cx| {
            loop {
                let Ok(Some((studio, run))) = this.read_with(cx, |t, _| t.studio.clone().map(|s| (s, t.run_id.clone()))) else {
                    return;
                };
                let got = cx.background_executor().spawn(async move { studio.view(&run) }).await;
                if this.update(cx, |t, cx| t.set_view(got, cx)).is_err() {
                    return;
                }
                cx.background_executor().timer(POLL).await;
            }
        }));
        // Keep the source following the output (clip lengths may differ from the source).
        tasks.push(cx.spawn_in(window, async move |this, cx| {
            loop {
                cx.background_executor().timer(SYNC).await;
                if this.update_in(cx, |t, window, cx| t.sync_tick(window, cx)).is_err() {
                    return;
                }
            }
        }));
        let status = if studio.is_some() { "connecting to c2a studio…".into() } else {
            "read-only: start `c2a studio` and pass --studio/--token for AI operations".into()
        };
        Self {
            run_dir, run_id, studio, view: None, status, source, output: None, scrub,
            scrubbing: false,
            canvas: Canvas { zoom: 0.8, pan: point(px(24.), px(24.)), drag_from: None, selected: None },
            _tasks: tasks,
        }
    }

    fn set_view(&mut self, got: anyhow::Result<View>, cx: &mut Context<Self>) {
        match got {
            Ok(v) => {
                self.status = match (&v.state[..], v.busy) {
                    (s, true) => format!("engine: {s}…"),
                    (s, false) => format!("engine: {s}"),
                };
                // (Re)open the output player when a new output appears.
                if let Some(out) = &v.output {
                    let path = media_path(&self.run_dir, out);
                    let stamp = format!("{out}@{:?}", std::fs::metadata(&path).and_then(|m| m.modified()).ok());
                    if self.output.as_ref().map(|o| &o.0) != Some(&stamp) && path.exists() {
                        let player = open_player(&path, cx);
                        cx.observe(&player, |_, _, cx| cx.notify()).detach();
                        self.output = Some((stamp, player));
                    }
                }
                self.view = Some(v);
            }
            Err(e) => self.status = format!("{e:#}"),
        }
        cx.notify();
    }

    // ---------------------------------------------------------------- transport
    fn duration(&self, cx: &App) -> f64 {
        self.output
            .as_ref()
            .and_then(|(_, p)| p.read(cx).duration())
            .or_else(|| self.source.read(cx).duration())
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    }

    fn position(&self, cx: &App) -> f64 {
        match &self.output {
            Some((_, p)) => p.read(cx).position().as_secs_f64(),
            None => self.source.read(cx).position().as_secs_f64(),
        }
    }

    fn mapped_source(&self, out_t: f64) -> f64 {
        match (&self.view, &self.output) {
            (Some(v), Some(_)) if !v.clips.is_empty() => v.source_time(out_t),
            _ => out_t,
        }
    }

    fn seek_fraction(&mut self, frac: f32, cx: &mut Context<Self>) {
        let t = frac as f64 * self.duration(cx);
        self.seek(t, cx);
    }

    fn seek(&mut self, t: f64, cx: &mut Context<Self>) {
        let src = self.mapped_source(t);
        if let Some((_, p)) = &self.output {
            p.update(cx, |p, cx| p.seek(Duration::from_secs_f64(t.max(0.0)), cx));
        }
        self.source.update(cx, |p, cx| p.seek(Duration::from_secs_f64(src.max(0.0)), cx));
        cx.notify();
    }

    fn toggle(&mut self, cx: &mut Context<Self>) {
        let playing = self.is_playing(cx);
        for p in self.players() {
            p.update(cx, |p, cx| if playing { p.pause(cx) } else { p.play(cx) });
        }
    }

    fn is_playing(&self, cx: &App) -> bool {
        self.players().iter().any(|p| p.read(cx).is_playing())
    }

    fn players(&self) -> Vec<Entity<VideoPlayer>> {
        let mut v = vec![self.source.clone()];
        if let Some((_, p)) = &self.output {
            v.push(p.clone());
        }
        v
    }

    fn sync_tick(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let dur = self.duration(cx);
        if dur > 0.0 && !self.scrubbing {
            let frac = (self.position(cx) / dur) as f32;
            self.scrub.update(cx, |s, cx| s.set_value(frac.clamp(0.0, 1.0), window, cx));
        }
        // Re-seek the source when it drifts from the clip mapping (lengths may differ).
        if self.output.is_some() && self.is_playing(cx) {
            let want = self.mapped_source(self.position(cx));
            let have = self.source.read(cx).position().as_secs_f64();
            if (want - have).abs() > 0.3 {
                self.source.update(cx, |p, cx| p.seek(Duration::from_secs_f64(want), cx));
            }
        }
        cx.notify();
    }

    // ---------------------------------------------------------------- AI operations
    fn command(&mut self, op: &'static str, args: serde_json::Value, cx: &mut Context<Self>) {
        let Some(studio) = self.studio.clone() else {
            self.status = "AI operations need --studio and --token".into();
            cx.notify();
            return;
        };
        let run = self.run_id.clone();
        self.status = format!("sending {op}…");
        cx.notify();
        cx.spawn(async move |this, cx| {
            let s2 = studio.clone();
            let r2 = run.clone();
            let res = cx.background_executor().spawn(async move { s2.command(&r2, op, args).and_then(|_| s2.view(&r2)) }).await;
            let _ = this.update(cx, |t, cx| {
                if let Err(e) = &res {
                    t.status = format!("{op}: {e:#}");
                }
                if let Ok(v) = res {
                    t.set_view(Ok(v), cx);
                }
                cx.notify();
            });
        })
        .detach();
    }

    // ---------------------------------------------------------------- rendering
    fn render_review(&mut self, cx: &mut Context<Self>) -> impl IntoElement {
        let playing = self.is_playing(cx);
        let dur = self.duration(cx);
        let pos = self.position(cx);
        let pane = |title: &str, body: AnyElement, cx: &App| {
            v_flex()
                .flex_1()
                .gap_1()
                .child(div().text_sm().text_color(cx.theme().muted_foreground).child(title.to_string()))
                .child(div().h(px(260.)).w_full().bg(gpui_kit::black()).rounded_md().overflow_hidden().child(body))
        };
        let out_body = match &self.output {
            Some((_, p)) => video_view(p.clone()).size_full().into_any_element(),
            None => div()
                .size_full()
                .flex()
                .items_center()
                .justify_center()
                .text_color(gpui_kit::white())
                .child("no output yet: approve a style sample to generate")
                .into_any_element(),
        };
        let clips = self.view.as_ref().map(|v| v.clips.clone()).unwrap_or_default();
        v_flex()
            .gap_2()
            .child(
                h_flex()
                    .gap_3()
                    .child(pane("Source", video_view(self.source.clone()).size_full().into_any_element(), cx))
                    .child(pane("Output (anime)", out_body, cx)),
            )
            .child(
                h_flex()
                    .gap_2()
                    .items_center()
                    .child(
                        Button::new("play")
                            .primary()
                            .small()
                            .label(if playing { "Pause" } else { "Play" })
                            .on_click(cx.listener(|this, _, _, cx| this.toggle(cx))),
                    )
                    .child(div().flex_1().child(Slider::new(&self.scrub)))
                    .child(div().text_sm().child(format!("{pos:5.2} / {dur:5.2} s"))),
            )
            .child(h_flex().gap_1().flex_wrap().children(clips.into_iter().map(|c| {
                let label = format!("{} · {:.1}s · {}", c.id, c.clip_s, c.status);
                let id = c.id.clone();
                let at = c.out.0;
                h_flex()
                    .gap_1()
                    .child(Button::new(SharedString::from(format!("go-{id}"))).small().outline().label(label)
                        .on_click(cx.listener(move |this, _, _, cx| this.seek(at, cx))))
                    .child(Button::new(SharedString::from(format!("rerun-{id}"))).small().ghost().label("↻")
                        .tooltip("re-run this clip with a new seed")
                        .on_click(cx.listener(move |this, _, _, cx| this.command("rerun", json!({"clip": id.clone()}), cx))))
            })))
    }

    fn render_canvas(&mut self, cx: &mut Context<Self>) -> impl IntoElement {
        let zoom = self.canvas.zoom;
        let pan = self.canvas.pan;
        let selected = self.canvas.selected.clone();
        let mut cards: Vec<AnyElement> = Vec::new();
        if let Some(v) = &self.view {
            let card = |i: usize, row: usize, path: PathBuf, title: String, sub: String, on: bool, sel: bool, id: Selected,
                        cx: &mut Context<Self>| {
                let (x, y) = (i as f32 * (CARD + 20.), row as f32 * (CARD * 0.75 + 30.));
                let w = CARD * zoom;
                div()
                    .id(SharedString::from(format!("card-{id:?}")))
                    .absolute()
                    .left(pan.x + px(x * zoom))
                    .top(pan.y + px(y * zoom))
                    .w(px(w))
                    .rounded_md()
                    .border_2()
                    .border_color(if sel { cx.theme().primary } else if on { gpui_kit::rgb(0xc2410c).into() } else { cx.theme().border })
                    .bg(cx.theme().background)
                    .overflow_hidden()
                    .child(img(path).w(px(w)).h(px(w * 9. / 16.)).object_fit(ObjectFit::Cover))
                    .child(v_flex().p_1().child(div().text_xs().child(title)).child(div().text_xs().text_color(cx.theme().muted_foreground).child(sub)))
                    .on_mouse_down(MouseButton::Left, cx.listener(move |this, _, _, cx| {
                        this.canvas.selected = Some(id.clone());
                        cx.stop_propagation();
                        cx.notify();
                    }))
                    .into_any_element()
            };
            let keys = v.keys.clone();
            for (i, k) in keys.iter().enumerate() {
                let path = k.thumb.as_ref().map(|t| media_path(&self.run_dir, t)).unwrap_or_default();
                let sel = selected == Some(Selected::Key(k.start));
                cards.push(card(i, 0, path, format!("{:.2}s · {}", k.t, k.action), format!("{} · {}", k.motion, k.camera),
                                k.checked, sel, Selected::Key(k.start), cx));
            }
            let samples = v.samples.clone();
            let approved = v.approved.clone();
            for (i, smp) in samples.iter().enumerate() {
                let sel = selected == Some(Selected::Sample(smp.id.clone()));
                let ok = approved.as_deref() == Some(&smp.id[..]);
                cards.push(card(i, 1, media_path(&self.run_dir, &smp.image), format!("sample {}", smp.id),
                                if ok { "approved".into() } else { String::new() }, ok, sel, Selected::Sample(smp.id.clone()), cx));
            }
        }
        div()
            .id("canvas")
            .relative()
            .flex_1()
            .min_h(px(320.))
            .rounded_md()
            .border_1()
            .border_color(cx.theme().border)
            .bg(cx.theme().muted)
            .overflow_hidden()
            .children(cards)
            .on_mouse_down(MouseButton::Left, cx.listener(|this, ev: &MouseDownEvent, _, cx| {
                this.canvas.drag_from = Some(ev.position);
                cx.notify();
            }))
            .on_mouse_up(MouseButton::Left, cx.listener(|this, _, _, cx| {
                this.canvas.drag_from = None;
                cx.notify();
            }))
            .on_mouse_move(cx.listener(|this, ev: &MouseMoveEvent, _, cx| {
                if let (Some(from), true) = (this.canvas.drag_from, ev.dragging()) {
                    this.canvas.pan = this.canvas.pan + (ev.position - from);
                    this.canvas.drag_from = Some(ev.position);
                    cx.notify();
                }
            }))
            .on_scroll(cx.listener(|this, ev: &ScrollEvent, _, cx| {
                let dy = f32::from(ev.delta.pixel_delta(px(16.)).y);
                let old = this.canvas.zoom;
                let new = (old * (1.0 + dy * 0.002)).clamp(0.2, 3.0);
                // zoom about the cursor
                let k = new / old;
                let p = ev.position;
                this.canvas.pan = point(p.x - (p.x - this.canvas.pan.x) * k, p.y - (p.y - this.canvas.pan.y) * k);
                this.canvas.zoom = new;
                cx.notify();
            }))
    }

    fn render_ops(&mut self, cx: &mut Context<Self>) -> impl IntoElement {
        let Some(v) = self.view.clone() else {
            return v_flex().child(div().text_sm().child("AI operations appear once the engine view loads."));
        };
        let mut panel = v_flex().gap_2().child(div().font_weight(FontWeight::SEMIBOLD).child("AI operations"));
        match &self.canvas.selected {
            Some(Selected::Key(start)) => {
                if let Some(k) = v.keys.iter().find(|k| k.start == *start).cloned() {
                    let start = k.start;
                    panel = panel
                        .child(div().text_sm().child(format!("Key image at {:.2}s: {} ({}), camera {}, importance {:.2}",
                                                            k.t, k.action, k.motion, k.camera, k.importance)))
                        .child(Checkbox::new("key-check").label(if k.locked { "starts a shot (always a clip)" } else { "make a clip from this image" })
                            .checked(k.checked)
                            .disabled(k.locked)
                            .on_click(cx.listener(move |this, checked: &bool, _, cx| {
                                this.command("toggle_key", json!({"start": start, "checked": *checked}), cx)
                            })));
                }
            }
            Some(Selected::Sample(id)) => {
                let id = id.clone();
                let done = v.approved.as_deref() == Some(&id[..]);
                panel = panel.child(div().text_sm().child(format!("Style sample {id}")))
                    .child(Button::new("approve").primary().small().label(if done { "Approved" } else { "Approve this style → generate" })
                        .disabled(done || !v.samples_current)
                        .on_click(cx.listener(move |this, _, _, cx| this.command("approve", json!({"sample": id.clone()}), cx))));
            }
            None => panel = panel.child(div().text_sm().text_color(cx.theme().muted_foreground)
                .child("Select a key image or a style sample on the canvas.")),
        }
        let directors = v.directors.clone();
        let current = v.director.clone();
        panel = panel
            .child(div().text_sm().child("Director's language"))
            .child(h_flex().gap_1().flex_wrap().children(directors.into_iter().map(|(name, title)| {
                let sel = current.as_deref() == Some(&name[..]);
                Button::new(SharedString::from(format!("dir-{name}"))).small().outline().selected(sel).label(title)
                    .on_click(cx.listener(move |this, _, _, cx| this.command("set_style", json!({"director": name.clone()}), cx)))
            })))
            .child(div().text_sm().child("Sound"))
            .child(h_flex().gap_1().children(["regenerate", "keep_voice", "silent"].into_iter().map(|m| {
                Button::new(SharedString::from(format!("audio-{m}"))).small().outline().selected(v.audio_mode == m).label(m.replace('_', " "))
                    .on_click(cx.listener(move |this, _, _, cx| this.command("set_audio", json!({"mode": m}), cx)))
            })));
        panel
    }
}

impl Render for ReviewApp {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let title = format!("c2a Review · {}", self.run_id);
        v_flex()
            .size_full()
            .bg(cx.theme().background)
            .text_color(cx.theme().foreground)
            .p_3()
            .gap_3()
            .child(
                h_flex()
                    .justify_between()
                    .child(div().font_weight(FontWeight::SEMIBOLD).child(title))
                    .child(div().text_sm().text_color(cx.theme().muted_foreground).child(self.status.clone())),
            )
            .child(self.render_review(cx))
            .child(
                h_flex()
                    .flex_1()
                    .gap_3()
                    .items_start()
                    .child(v_flex().flex_1().h_full().gap_1()
                        .child(div().text_sm().text_color(cx.theme().muted_foreground)
                            .child("Canvas · drag to pan · wheel to zoom · click to select"))
                        .child(self.render_canvas(cx)))
                    .child(div().w(px(380.)).child(self.render_ops(cx))),
            )
    }
}

fn arg(name: &str) -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned())
}

fn main() {
    let Some(run_dir) = arg("--run-dir").map(PathBuf::from) else {
        eprintln!("usage: c2a-review --run-dir runs/<id> [--studio http://127.0.0.1:8765 --token T]");
        std::process::exit(2);
    };
    let studio = arg("--studio").map(|base| Studio::new(&base, &arg("--token").unwrap_or_default()));
    gpui_kit::application().run(move |cx| {
        gpui_kit::init(cx);
        let options = WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(Bounds::centered(None, size(px(1280.), px(800.)), cx))),
            ..Default::default()
        };
        gpui_kit::open_window(options, cx, move |window, cx| cx.new(|cx| ReviewApp::new(run_dir, studio, window, cx)))
            .expect("failed to open window");
    });
}
