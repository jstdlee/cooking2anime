//! c2a Review: a native desktop app on GPUI Kit (research branch).
//!
//! Double-click it (or run `c2a-review`): it starts the c2a engine itself and shows your
//! runs. Optional arguments: `--workdir <runs folder>`, `--run-dir <run folder>`,
//! `--c2a <path to c2a>`, or `--studio <url> --token <t>` to use an engine already running.
//!
//! - Start screen: the runs in the runs folder, new run from a video, open a run folder,
//!   change the runs folder, and the engine's state.
//! - Review: source and output video side by side with one transport (play/pause,
//!   scrub on the output timeline, the source follows the clip mapping) and a clip list.
//! - Canvas: key images and style samples on a pannable, zoomable surface (drag, wheel).
//! - AI operations: check/uncheck key images, approve a sample, re-run a clip, switch the
//!   director preset or sound mode. Each is an engine command sent to `c2a studio`.

mod engine;
mod runs;
mod ui;
mod settings;
mod studio;
mod video;

use std::path::{Path, PathBuf};
use std::time::Duration;

use gpui_kit::component::button::*;
use gpui_kit::component::checkbox::Checkbox;
use gpui_kit::component::slider::{Slider, SliderEvent, SliderState};
use gpui_kit::component::{h_flex, v_flex, ActiveTheme as _, Disableable as _, Selectable as _, Sizable as _};
use gpui_kit::*;
use gpui_kit::prelude::FluentBuilder as _;
use serde_json::json;

use gpui_kit::component::input::{Input, InputEvent, InputState};
use gpui_kit::component::progress::Progress;
use gpui_kit::component::switch::Switch;
use gpui_kit::component::{Theme, ThemeMode};
use runs::RunInfo;
use settings::Settings;
use studio::{Studio, View, media_path};
use video::{PlayerConfig, Source, VideoPlayer, video_view};

const READ_ONLY: &str = "read-only: the engine is not running (see the start screen)";
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

/// One run: review (video), canvas (key images, samples) and AI operations.
struct RunView {
    run_dir: PathBuf,
    run_id: String,
    studio: Option<Studio>,
    view: Option<View>,
    status: String,
    /// Opened once the engine has prepared the source video (a new run starts without it).
    source: Option<Entity<VideoPlayer>>,
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

/// Sent to the shell when the user goes back to the start screen.
enum RunViewEvent {
    Back,
    OpenSettings,
}

impl EventEmitter<RunViewEvent> for RunView {}

impl RunView {
    fn new(run_dir: PathBuf, studio: Option<Studio>, window: &mut Window, cx: &mut Context<Self>) -> Self {
        let run_id = run_dir.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        let scrub = cx.new(|_| SliderState::new().min(0.0).max(1.0).step(0.001));
        cx.subscribe_in(&scrub, window, |this: &mut Self, _, ev: &SliderEvent, _, cx| {
            let (SliderEvent::Change(v) | SliderEvent::Release(v)) = ev;
            this.scrubbing = matches!(ev, SliderEvent::Change(_));
            this.seek_fraction(v.start(), cx);
        })
        .detach();

        let mut tasks = Vec::new();
        // Poll the engine's view (state, keys, samples, clips) while the run is open; without
        // an engine, show the run's videos from disk.
        tasks.push(cx.spawn(async move |this, cx| {
            loop {
                let Ok((studio, run)) = this.read_with(cx, |t, _| (t.studio.clone(), t.run_id.clone())) else {
                    return;
                };
                let alive = match studio {
                    Some(studio) => {
                        let got = cx.background_executor().spawn(async move { studio.view(&run) }).await;
                        this.update(cx, |t, cx| t.set_view(got, cx)).is_ok()
                    }
                    None => this.update(cx, |t, cx| t.refresh_from_disk(cx)).is_ok(),
                };
                if !alive {
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
        let status = if studio.is_some() { "connecting to the engine…".into() } else { READ_ONLY.into() };
        let mut this = Self {
            run_dir, run_id, studio, view: None, status, source: None, output: None, scrub,
            scrubbing: false,
            canvas: Canvas { zoom: 0.8, pan: point(px(24.), px(24.)), drag_from: None, selected: None },
            _tasks: tasks,
        };
        this.ensure_source(cx);
        this
    }

    /// The engine connection changed (started, restarted or stopped).
    fn set_studio(&mut self, studio: Option<Studio>, cx: &mut Context<Self>) {
        if studio.is_none() {
            self.view = None;
            self.status = READ_ONLY.into();
        }
        self.studio = studio;
        cx.notify();
    }

    fn ensure_source(&mut self, cx: &mut Context<Self>) {
        let path = self.run_dir.join("ingest/normalized.mp4");
        if self.source.is_none() && path.is_file() {
            let player = open_player(&path, cx);
            cx.observe(&player, |_, _, cx| cx.notify()).detach();
            self.source = Some(player);
        }
    }

    fn set_output_file(&mut self, path: PathBuf, cx: &mut Context<Self>) {
        let stamp = format!("{}@{:?}", path.display(), std::fs::metadata(&path).and_then(|m| m.modified()).ok());
        if self.output.as_ref().map(|o| &o.0) != Some(&stamp) && path.is_file() {
            let player = open_player(&path, cx);
            cx.observe(&player, |_, _, cx| cx.notify()).detach();
            self.output = Some((stamp, player));
        }
    }

    fn refresh_from_disk(&mut self, cx: &mut Context<Self>) {
        self.ensure_source(cx);
        if let Some(out) = runs::latest_output(&self.run_dir) {
            self.set_output_file(out, cx);
        }
        cx.notify();
    }

    fn set_view(&mut self, got: anyhow::Result<View>, cx: &mut Context<Self>) {
        match got {
            Ok(v) => {
                self.status = match (&v.state[..], v.busy) {
                    (s, true) => format!("engine: {s}…"),
                    (s, false) => format!("engine: {s}"),
                };
                self.ensure_source(cx);
                // (Re)open the output player when a new output appears.
                if let Some(out) = &v.output {
                    self.set_output_file(media_path(&self.run_dir, out), cx);
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
            .or_else(|| self.source.as_ref().and_then(|s| s.read(cx).duration()))
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    }

    fn position(&self, cx: &App) -> f64 {
        match &self.output {
            Some((_, p)) => p.read(cx).position().as_secs_f64(),
            None => self.source.as_ref().map(|s| s.read(cx).position().as_secs_f64()).unwrap_or(0.0),
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
        if let Some(s) = &self.source {
            s.update(cx, |p, cx| p.seek(Duration::from_secs_f64(src.max(0.0)), cx));
        }
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
        let mut v: Vec<Entity<VideoPlayer>> = self.source.iter().cloned().collect();
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
        if let (Some(source), true) = (self.source.clone(), self.output.is_some() && self.is_playing(cx)) {
            let want = self.mapped_source(self.position(cx));
            let have = source.read(cx).position().as_secs_f64();
            if (want - have).abs() > 0.3 {
                source.update(cx, |p, cx| p.seek(Duration::from_secs_f64(want), cx));
            }
        }
        cx.notify();
    }

    // ---------------------------------------------------------------- AI operations
    fn command(&mut self, op: &'static str, args: serde_json::Value, cx: &mut Context<Self>) {
        let Some(studio) = self.studio.clone() else {
            self.status = "AI operations need the engine (see the start screen)".into();
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
        let waiting = |msg: &'static str| {
            div().size_full().flex().items_center().justify_center().text_color(gpui_kit::white()).child(msg).into_any_element()
        };
        let src_body = match &self.source {
            Some(s) => video_view(s.clone()).size_full().into_any_element(),
            None => waiting("preparing the source video…"),
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
                    .child(pane("Source", src_body, cx))
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

impl RunView {
    fn render_status(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let Some(v) = self.view.clone() else {
            return div().text_sm().text_color(cx.theme().muted_foreground).child(self.status.clone()).into_any_element();
        };
        let mut col = v_flex().gap_2().child(ui::stepper(&v.state, cx));
        if v.state == "failed" {
            let note = v.note.clone().unwrap_or_else(|| "the engine stopped with an error".into());
            col = col.child(
                h_flex().gap_3().p_3().rounded_lg().items_center()
                    .bg(ui::tint(ui::RED, 0.12)).border_1().border_color(ui::tint(ui::RED, 0.5))
                    .child(v_flex().flex_1().min_w_0().gap_1()
                        .child(div().font_weight(FontWeight::SEMIBOLD).text_color(ui::rgb(ui::RED)).child("This run stopped"))
                        .child(div().text_sm().child(note)))
                    .child(Button::new("settings").outline().small().label("AI settings…")
                        .on_click(cx.listener(|_, _, _, cx| cx.emit(RunViewEvent::OpenSettings))))
                    .child(Button::new("retry").primary().small().label("Retry")
                        .on_click(cx.listener(|this, _, _, cx| this.command("resume", json!({}), cx)))),
            );
        } else if v.busy || !matches!(v.state.as_str(), "done" | "awaiting_approval") {
            let (stage, unit, pct) = v.progress.clone().unwrap_or((v.state.clone(), String::new(), 0.0));
            col = col.child(
                h_flex().gap_3().items_center()
                    .child(div().w(px(260.)).child(Progress::new("prog").value(pct as f32)))
                    .child(div().text_sm().text_color(cx.theme().muted_foreground)
                        .child(format!("{} {}{}", ui::stage_title(&stage), unit, if pct > 0.0 { format!(" · {pct:.0}%") } else { String::new() }))),
            );
        } else if v.state == "awaiting_approval" {
            col = col.child(
                h_flex().gap_2().p_2().rounded_lg().bg(ui::tint(ui::AMBER, 0.12))
                    .child(div().text_sm().child("Your turn: pick the style sample you like on the canvas below (second row), then “Approve this style → generate”.")),
            );
        }
        col.into_any_element()
    }
}

impl Render for RunView {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let source = self.view.as_ref().map(|v| v.source.clone()).filter(|s| !s.is_empty()).unwrap_or_else(|| self.run_id.clone());
        v_flex()
            .id("run-view")
            .size_full()
            .overflow_y_scroll()
            .gap_3()
            .child(
                h_flex()
                    .justify_between()
                    .items_center()
                    .child(h_flex().gap_3().items_center()
                        .child(Button::new("back").small().ghost().label("← Runs")
                            .on_click(cx.listener(|_, _, _, cx| cx.emit(RunViewEvent::Back))))
                        .child(div().text_lg().font_weight(FontWeight::SEMIBOLD).child(source))
                        .child(div().text_sm().text_color(cx.theme().muted_foreground).child(format!("run {}", self.run_id))))
                    .child(div().text_xs().text_color(cx.theme().muted_foreground).child(self.status.clone())),
            )
            .child(self.render_status(cx))
            .child(self.render_review(cx))
            .child(
                h_flex()
                    .gap_3()
                    .items_start()
                    .child(v_flex().flex_1().gap_1()
                        .child(div().text_sm().text_color(cx.theme().muted_foreground)
                            .child("Key images (top) and style samples (bottom) · drag to pan · wheel to zoom · click to select"))
                        .child(div().h(px(340.)).child(self.render_canvas(cx))))
                    .child(div().w(px(360.)).p_3().rounded_lg().bg(ui::rgb(ui::CARD)).border_1().border_color(ui::rgb(ui::LINE)).child(self.render_ops(cx))),
            )
    }
}

// ==================================================================== the app shell

/// The engine (`c2a studio`) as the app sees it.
#[derive(Clone)]
enum Engine {
    /// Not running; the reason is shown in the sidebar and on the settings page.
    Off(String),
    Starting,
    /// Started by this app (stopped when the app quits).
    Ours(Studio),
    /// Given with --studio/--token: someone else runs it.
    External(Studio),
}

impl Engine {
    fn studio(&self) -> Option<Studio> {
        match self {
            Engine::Ours(s) | Engine::External(s) => Some(s.clone()),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Page {
    Runs,
    New,
    Settings,
}

/// The New-run form.
struct NewRunForm {
    video: Option<PathBuf>,
    animation: Option<String>,
    director: Option<String>,
    finish: Option<String>,
    resolution: &'static str,
    audio: &'static str,
}

/// The Settings form: text fields live in input entities, the rest here.
struct SettingsForm {
    mode: settings::Mode,
    voice_enabled: bool,
    video_resolution: String,
    video_backend: String,
    comfy_url: Entity<InputState>,
    vlm_base: Entity<InputState>,
    vlm_model: Entity<InputState>,
    vlm_key: Entity<InputState>,
    fal_key: Entity<InputState>,
    fal_video: Entity<InputState>,
    fal_image: Entity<InputState>,
    fish_key: Entity<InputState>,
    voice_model: Entity<InputState>,
    voice_ref: Entity<InputState>,
    checks: Vec<studio::Check>,
    checking: bool,
}

/// The window: a sidebar and one page (runs, new run, settings) or one open run.
struct Shell {
    settings: Settings,
    workdir: PathBuf,
    engine: Engine,
    c2a: Option<PathBuf>,
    runs: Vec<RunInfo>,
    catalog: studio::Catalog,
    page: Page,
    open: Option<Entity<RunView>>,
    /// The last thing that happened (or went wrong).
    notice: Option<(String, u32)>,
    /// "uploading …" while a new run is being created.
    busy: Option<String>,
    path_box: Entity<InputState>,
    /// The last "Save and check" result: Some(true) when every check passed.
    verified: Option<bool>,
    new_run: NewRunForm,
    form: SettingsForm,
    _tasks: Vec<Task<()>>,
}

fn input(window: &mut Window, cx: &mut App, value: &str, placeholder: &str, masked: bool) -> Entity<InputState> {
    let value = value.to_string();
    let placeholder = placeholder.to_string();
    cx.new(|cx| {
        let mut s = InputState::new(window, cx).placeholder(placeholder).masked(masked);
        s.set_value(value, window, cx);
        s
    })
}

impl Shell {
    fn new(args: Args, window: &mut Window, cx: &mut Context<Self>) -> Self {
        let mut settings = Settings::load();
        if let Some(c) = &args.c2a {
            settings.c2a = Some(c.clone());
        }
        let workdir = args
            .workdir
            .clone()
            .or_else(|| args.run_dir.as_ref().and_then(|d| d.parent().map(Path::to_path_buf)))
            .unwrap_or_else(|| settings.workdir_or_default());
        let path_box = cx.new(|cx| InputState::new(window, cx).placeholder("…or paste the path of a video file here and press Enter"));
        cx.subscribe_in(&path_box, window, |this: &mut Self, _, ev: &InputEvent, window, cx| {
            if let InputEvent::PressEnter { .. } = ev {
                this.go_path_box(window, cx);
            }
        })
        .detach();
        let form = Self::form_from(&settings, window, cx);
        let mut tasks = Vec::new();
        // Keep the run list current (new runs, state changes), read from disk.
        tasks.push(cx.spawn(async move |this, cx| {
            loop {
                if this.update(cx, |t, cx| t.refresh_runs(cx)).is_err() {
                    return;
                }
                cx.background_executor().timer(Duration::from_secs(2)).await;
            }
        }));
        let first_launch = !settings.configured;
        let mut this = Self {
            settings, workdir, engine: Engine::Off(String::new()), c2a: None, runs: Vec::new(),
            catalog: studio::Catalog::default(), page: if first_launch { Page::Settings } else { Page::Runs },
            open: None, notice: None, busy: None, path_box, verified: None,
            new_run: NewRunForm { video: None, animation: None, director: None, finish: None, resolution: "720p", audio: "regenerate" },
            form, _tasks: tasks,
        };
        match args.studio {
            Some(s) => this.set_engine(Engine::External(s), cx),
            None => this.start_engine(cx),
        }
        if let Some(dir) = args.run_dir {
            this.open_run(dir, window, cx);
        }
        this.refresh_runs(cx);
        this
    }

    fn form_from(s: &Settings, window: &mut Window, cx: &mut Context<Self>) -> SettingsForm {
        SettingsForm {
            mode: s.mode,
            voice_enabled: s.voice_enabled,
            video_resolution: s.fal_video_resolution.clone(),
            video_backend: s.video_backend.clone(),
            comfy_url: input(window, cx, &s.comfy_url, "http://dgx:8188", false),
            vlm_base: input(window, cx, &s.vlm_base_url, "https://…/v1/", false),
            vlm_model: input(window, cx, &s.vlm_model, "model name", false),
            vlm_key: input(window, cx, &s.vlm_key, "API key", true),
            fal_key: input(window, cx, &s.fal_key, "fal.ai API key", true),
            fal_video: input(window, cx, &s.fal_video_model, "fal model id", false),
            fal_image: input(window, cx, &s.fal_image_model, "fal model id", false),
            fish_key: input(window, cx, &s.fish_key, "Fish Audio API key", true),
            voice_model: input(window, cx, &s.voice_model, "voice model", false),
            voice_ref: input(window, cx, &s.voice_reference, "optional: a Fish voice id", false),
            checks: Vec::new(),
            checking: false,
        }
    }

    fn refresh_runs(&mut self, cx: &mut Context<Self>) {
        let runs = runs::list(&self.workdir);
        if runs != self.runs {
            self.runs = runs;
            cx.notify();
        }
    }

    fn say(&mut self, text: impl Into<String>, color: u32, cx: &mut Context<Self>) {
        self.notice = Some((text.into(), color));
        cx.notify();
    }

    // ---------------------------------------------------------------- engine
    fn start_engine(&mut self, cx: &mut Context<Self>) {
        if matches!(self.engine, Engine::External(_)) {
            return;
        }
        self.c2a = engine::find_c2a(self.settings.c2a.as_deref());
        let Some(c2a) = self.c2a.clone() else {
            self.set_engine(Engine::Off("the c2a engine was not found — unpack the c2a release next to this app or in your home folder, or use “Locate c2a…” in Settings".into()), cx);
            return;
        };
        self.set_engine(Engine::Starting, cx);
        let mut args = Vec::new();
        if let Some(cfg) = self.settings.write_engine_config() {
            args.push("-c".to_string());
            args.push(cfg.to_string_lossy().to_string());
        }
        args.extend(self.settings.engine_args.clone());
        let env = self.settings.engine_config().1;
        let workdir = self.workdir.clone();
        cx.spawn(async move |this, cx| {
            let got = cx.background_executor().spawn(async move { engine::start(&c2a, &workdir, &args, &env) }).await;
            let _ = this.update(cx, |t, cx| match got {
                Ok(studio) => {
                    t.set_engine(Engine::Ours(studio), cx);
                    t.load_catalog(cx);
                }
                Err(e) => t.set_engine(Engine::Off(format!("{e:#}")), cx),
            });
        })
        .detach();
    }

    fn set_engine(&mut self, engine: Engine, cx: &mut Context<Self>) {
        let studio = engine.studio();
        self.engine = engine;
        if let Some(open) = &self.open {
            open.update(cx, |r, cx| r.set_studio(studio, cx));
        }
        cx.notify();
    }

    fn load_catalog(&mut self, cx: &mut Context<Self>) {
        let Some(studio) = self.engine.studio() else { return };
        cx.spawn(async move |this, cx| {
            let got = cx.background_executor().spawn(async move { studio.runs() }).await;
            if let Ok(v) = got {
                let _ = this.update(cx, |t, cx| {
                    t.catalog = studio::Catalog::from_json(v.get("catalog").unwrap_or(&serde_json::Value::Null));
                    cx.notify();
                });
            }
        })
        .detach();
    }

    /// (sidebar line, color) for the engine and the AI providers.
    fn engine_label(&self) -> (String, u32) {
        let mode = if self.settings.mode == settings::Mode::Demo { "Demo mode (no AI)" } else { "Cloud AI" };
        match &self.engine {
            Engine::Off(why) if why.is_empty() => ("engine off".into(), ui::MUTED),
            Engine::Off(_) => ("engine off — see Settings".into(), ui::RED),
            Engine::Starting => ("engine starting…".into(), ui::AMBER),
            Engine::Ours(_) | Engine::External(_) => {
                if self.settings.mode == settings::Mode::Demo {
                    return (format!("{mode} · ready"), ui::GREEN);
                }
                match (self.settings.missing().is_empty(), self.verified) {
                    (false, _) => (format!("{mode} · needs keys"), ui::AMBER),
                    (true, Some(true)) => (format!("{mode} · ready"), ui::GREEN),
                    (true, Some(false)) => (format!("{mode} · check failed"), ui::RED),
                    (true, None) => (format!("{mode} · not checked yet"), ui::AMBER),
                }
            }
        }
    }

    // ---------------------------------------------------------------- navigation and actions
    fn go(&mut self, page: Page, cx: &mut Context<Self>) {
        self.page = page;
        self.open = None;
        self.notice = None;
        cx.notify();
    }

    fn open_run(&mut self, dir: PathBuf, window: &mut Window, cx: &mut Context<Self>) {
        let studio = self.engine.studio();
        let view = cx.new(|cx| RunView::new(dir, studio, window, cx));
        cx.subscribe(&view, |this: &mut Self, _, ev: &RunViewEvent, cx| match ev {
            RunViewEvent::Back => this.go(Page::Runs, cx),
            RunViewEvent::OpenSettings => this.go(Page::Settings, cx),
        })
        .detach();
        self.open = Some(view);
        self.notice = None;
        cx.notify();
    }

    fn set_workdir(&mut self, dir: PathBuf, cx: &mut Context<Self>) {
        self.workdir = dir.clone();
        self.settings.workdir = Some(dir);
        self.settings.save();
        self.runs.clear();
        self.refresh_runs(cx);
        self.start_engine(cx); // the engine serves one runs folder
    }

    fn set_c2a(&mut self, path: PathBuf, cx: &mut Context<Self>) {
        self.settings.c2a = Some(path);
        self.settings.save();
        self.start_engine(cx);
    }

    fn start_new_run(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let Some(video) = self.new_run.video.clone() else {
            self.say("choose a video first", ui::AMBER, cx);
            return;
        };
        let missing = self.settings.missing();
        if !missing.is_empty() {
            self.say(format!("Cloud AI needs {} — add it in Settings (or switch to Demo mode).", missing.join(", ")), ui::AMBER, cx);
            return;
        }
        let Some(studio) = self.engine.studio() else {
            self.say("the engine is not running yet — see the sidebar", ui::AMBER, cx);
            return;
        };
        let f = &self.new_run;
        let mut selection = serde_json::Map::new();
        for (k, v) in [("animation", &f.animation), ("director", &f.director), ("finish", &f.finish)] {
            if let Some(v) = v {
                selection.insert(k.into(), json!(v));
            }
        }
        let mut run_settings = json!({"output": {"resolution": f.resolution}, "audio": {"mode": f.audio}});
        if !selection.is_empty() {
            run_settings["style"] = json!({"selection": selection});
        }
        self.busy = Some(format!("Uploading {} to the engine…", video.file_name().unwrap_or_default().to_string_lossy()));
        cx.notify();
        cx.spawn_in(window, async move |this, cx| {
            let got = cx.background_executor().spawn(async move { studio.create_run(&video, &run_settings) }).await;
            let _ = this.update_in(cx, |t, window, cx| {
                t.busy = None;
                match got {
                    Ok(id) => {
                        t.new_run.video = None;
                        let dir = t.workdir.join(&id);
                        t.open_run(dir, window, cx);
                    }
                    Err(e) => t.say(format!("Could not start the run: {e:#}"), ui::RED, cx),
                }
                cx.notify();
            });
        })
        .detach();
    }

    /// Opens the system file dialog; `then` gets the chosen path. Without a desktop portal
    /// (some minimal Linux sessions) it says so and points to the path box instead.
    fn pick(&mut self, files: bool, prompt: &'static str, window: &mut Window, cx: &mut Context<Self>,
            then: impl FnOnce(&mut Self, PathBuf, &mut Window, &mut Context<Self>) + 'static) {
        let rx = cx.prompt_for_paths(PathPromptOptions { files, directories: !files, multiple: false, prompt: Some(prompt.into()) });
        cx.spawn_in(window, async move |this, cx| {
            let got = rx.await;
            let _ = this.update_in(cx, |t, window, cx| {
                match got {
                    Ok(Ok(Some(paths))) => {
                        if let Some(p) = paths.into_iter().next() {
                            then(t, p, window, cx);
                        }
                    }
                    Ok(Ok(None)) => {}
                    Ok(Err(_)) | Err(_) => t.say("No file dialog is available on this desktop: paste the path in the box instead.", ui::AMBER, cx),
                }
                cx.notify();
            });
        })
        .detach();
    }

    fn go_path_box(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let raw = self.path_box.read(cx).value().trim().trim_matches(|c| c == '"' || c == '\'').to_string();
        if raw.is_empty() {
            return;
        }
        let p = PathBuf::from(shellexpand_home(&raw));
        if runs::is_video(&p) && p.is_file() {
            self.new_run.video = Some(p);
            self.page = Page::New;
            self.open = None;
        } else if p.join("manifest.json").is_file() {
            self.open_run(p, window, cx);
        } else if p.is_dir() {
            self.set_workdir(p, cx);
        } else {
            self.say(format!("Not a video file, run folder or folder: {}", p.display()), ui::AMBER, cx);
        }
        self.path_box.update(cx, |s, cx| s.set_value("", window, cx));
        cx.notify();
    }

    // ---------------------------------------------------------------- settings
    fn apply_preset(&mut self, i: usize, window: &mut Window, cx: &mut Context<Self>) {
        let p = settings::VLM_PRESETS[i];
        self.form.vlm_base.update(cx, |s, cx| s.set_value(p.base_url, window, cx));
        self.form.vlm_model.update(cx, |s, cx| s.set_value(p.model, window, cx));
        cx.notify();
    }

    fn save_settings(&mut self, cx: &mut Context<Self>) {
        let v = |e: &Entity<InputState>, cx: &App| e.read(cx).value().trim().to_string();
        let f = &self.form;
        let s = &mut self.settings;
        s.mode = f.mode;
        s.voice_enabled = f.voice_enabled;
        s.fal_video_resolution = f.video_resolution.clone();
        s.video_backend = f.video_backend.clone();
        s.comfy_url = v(&f.comfy_url, cx);
        s.vlm_base_url = v(&f.vlm_base, cx);
        s.vlm_model = v(&f.vlm_model, cx);
        s.vlm_key = v(&f.vlm_key, cx);
        s.fal_key = v(&f.fal_key, cx);
        s.fal_video_model = v(&f.fal_video, cx);
        s.fal_image_model = v(&f.fal_image, cx);
        s.fish_key = v(&f.fish_key, cx);
        s.voice_model = v(&f.voice_model, cx);
        s.voice_reference = v(&f.voice_ref, cx);
        s.configured = true;
        s.save();
        self.form.checks.clear();
        self.form.checking = true;
        self.start_engine(cx);
        // Check once the engine is up (live: one tiny call to the vision model).
        cx.spawn(async move |this, cx| {
            for _ in 0..240 {
                cx.background_executor().timer(Duration::from_millis(250)).await;
                let Ok(engine) = this.read_with(cx, |t, _| t.engine.clone()) else { return };
                match engine {
                    Engine::Starting => continue,
                    Engine::Off(why) => {
                        let _ = this.update(cx, |t, cx| {
                            t.form.checking = false;
                            t.verified = Some(false);
                            t.form.checks = vec![studio::Check { name: "engine".into(), ok: false, detail: why, fix: "Locate c2a… below".into() }];
                            cx.notify();
                        });
                        return;
                    }
                    Engine::Ours(studio) | Engine::External(studio) => {
                        let got = cx.background_executor().spawn(async move { studio.doctor(true) }).await;
                        let _ = this.update(cx, |t, cx| {
                            t.form.checking = false;
                            match got {
                                Ok(checks) => {
                                    t.verified = Some(checks.iter().all(|c| c.ok));
                                    t.form.checks = checks;
                                }
                                Err(e) => t.form.checks = vec![studio::Check { name: "engine".into(), ok: false, detail: format!("{e:#}"), fix: String::new() }],
                            }
                            cx.notify();
                        });
                        return;
                    }
                }
            }
        })
        .detach();
        cx.notify();
    }

    // ---------------------------------------------------------------- rendering
    fn render_sidebar(&mut self, cx: &mut Context<Self>) -> impl IntoElement {
        let active = if self.open.is_some() { None } else { Some(self.page) };
        let item = |id: &'static str, label: &'static str, page: Page, active: Option<Page>, cx: &mut Context<Self>| {
            let on = active == Some(page);
            div()
                .id(id)
                .px_3()
                .py_2()
                .rounded_lg()
                .cursor_pointer()
                .text_sm()
                .font_weight(if on { FontWeight::SEMIBOLD } else { FontWeight::NORMAL })
                .text_color(if on { ui::rgb(0xffffff) } else { ui::rgb(0xc4c4d0) })
                .when(on, |d| d.bg(ui::tint(ui::ACCENT, 0.16)).border_l_2().border_color(ui::rgb(ui::ACCENT)))
                .hover(|d| d.bg(ui::rgb(ui::CARD_HOVER)))
                .child(label)
                .on_click(cx.listener(move |this, _, _, cx| this.go(page, cx)))
        };
        let (engine_text, engine_color) = self.engine_label();
        v_flex()
            .w(px(220.))
            .h_full()
            .flex_none()
            .bg(ui::rgb(ui::SIDEBAR))
            .border_r_1()
            .border_color(ui::rgb(ui::LINE))
            .p_3()
            .gap_1()
            .child(
                v_flex().px_2().pt_2().pb_4().gap_1()
                    .child(h_flex().gap_2().items_center()
                        .child(div().size(px(22.)).rounded_md().bg(ui::rgb(ui::ACCENT))
                            .child(div().size_full().rounded_md().bg(ui::tint(ui::ACCENT_2, 0.55))))
                        .child(div().text_lg().font_weight(FontWeight::BOLD).child("c2a Review")))
                    .child(div().text_xs().text_color(ui::rgb(ui::MUTED)).child("cooking video → anime")),
            )
            .child(item("nav-runs", "Runs", Page::Runs, active, cx))
            .child(item("nav-new", "New run", Page::New, active, cx))
            .child(item("nav-settings", "Settings", Page::Settings, active, cx))
            .child(div().flex_1())
            .child(
                v_flex().gap_1().p_3().rounded_lg().bg(ui::rgb(ui::CARD)).border_1().border_color(ui::rgb(ui::LINE))
                    .child(h_flex().gap_2().items_center()
                        .child(div().size(px(8.)).rounded_full().bg(ui::rgb(engine_color)))
                        .child(div().text_xs().font_weight(FontWeight::MEDIUM).child(engine_text)))
                    .child(div().text_xs().text_color(ui::rgb(ui::MUTED)).truncate().child(format!("{}", self.workdir.display()))),
            )
    }

    fn render_notice(&self) -> Option<AnyElement> {
        let (text, color) = self.busy.clone().map(|b| (b, ui::BLUE)).or_else(|| self.notice.clone())?;
        Some(div().p_3().rounded_lg().text_sm().bg(ui::tint(color, 0.12)).border_1().border_color(ui::tint(color, 0.45)).child(text).into_any_element())
    }

    fn render_runs(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let header = h_flex().justify_between().items_center()
            .child(v_flex().gap_1()
                .child(div().text_2xl().font_weight(FontWeight::BOLD).child("Runs"))
                .child(div().text_sm().text_color(ui::rgb(ui::MUTED)).child(format!("{} in {}", self.runs.len(), self.workdir.display()))))
            .child(h_flex().gap_2()
                .child(Button::new("folder").outline().label("Change folder…").on_click(cx.listener(|this, _, window, cx| {
                    this.pick(false, "Choose the runs folder", window, cx, |t, p, _, cx| t.set_workdir(p, cx))
                })))
                .child(Button::new("new").primary().label("New run").on_click(cx.listener(|this, _, _, cx| this.go(Page::New, cx)))));
        if self.runs.is_empty() {
            return v_flex().gap_6().child(header).child(
                v_flex().items_center().justify_center().gap_3().py_16().rounded_xl().border_1().border_dashed().border_color(ui::rgb(ui::LINE))
                    .child(div().text_xl().font_weight(FontWeight::SEMIBOLD).child("Turn a cooking video into anime"))
                    .child(div().text_sm().text_color(ui::rgb(ui::MUTED)).child("Start a run from a video: the AI picks key images, tags the action, and shows style samples for you to approve."))
                    .child(Button::new("empty-new").primary().label("New run").on_click(cx.listener(|this, _, _, cx| this.go(Page::New, cx)))),
            ).into_any_element();
        }
        let cards: Vec<AnyElement> = self.runs.clone().into_iter().map(|r| {
            let dir = r.dir.clone();
            let (label, color) = ui::state_chip(&r.state);
            let thumb = match &r.thumb {
                Some(p) => img(p.clone()).w_full().h(px(162.)).object_fit(ObjectFit::Cover).into_any_element(),
                None => div().w_full().h(px(162.)).bg(ui::tint(ui::ACCENT, 0.08)).flex().items_center().justify_center()
                    .text_sm().text_color(ui::rgb(ui::MUTED)).child("preparing…").into_any_element(),
            };
            v_flex()
                .id(SharedString::from(format!("run-{}", r.id)))
                .w(px(288.))
                .rounded_xl()
                .overflow_hidden()
                .bg(ui::rgb(ui::CARD))
                .border_1()
                .border_color(ui::rgb(ui::LINE))
                .cursor_pointer()
                .hover(|d| d.border_color(ui::rgb(ui::ACCENT)).bg(ui::rgb(ui::CARD_HOVER)))
                .child(thumb)
                .child(v_flex().p_3().gap_2()
                    .child(h_flex().justify_between().items_center().gap_2()
                        .child(div().flex_1().min_w_0().truncate().font_weight(FontWeight::SEMIBOLD).child(r.source.clone()))
                        .child(ui::chip(label, color)))
                    .child(div().text_xs().text_color(ui::rgb(ui::MUTED)).child(if r.created.is_empty() { r.id.clone() } else { r.created.clone() })))
                .on_click(cx.listener(move |this, _, window, cx| this.open_run(dir.clone(), window, cx)))
                .into_any_element()
        }).collect();
        v_flex().gap_6().child(header).child(h_flex().flex_wrap().gap_4().children(cards)).into_any_element()
    }

    fn render_new(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let demo = self.settings.mode == settings::Mode::Demo;
        let missing = self.settings.missing();
        let video = self.new_run.video.clone();
        let mut col = v_flex().w_full().gap_4().max_w(px(920.))
            .child(v_flex().gap_1()
                .child(div().text_2xl().font_weight(FontWeight::BOLD).child("New run"))
                .child(div().text_sm().text_color(ui::rgb(ui::MUTED)).child("Choose a cooking video and a look. The AI finds the key moments and makes style samples; you approve one, then it animates the whole video.")));
        if demo {
            col = col.child(div().p_3().rounded_lg().text_sm().bg(ui::tint(ui::AMBER, 0.12)).child("Demo mode: no AI is called and the result is a quick stand-in. Switch to Cloud AI in Settings for real anime output."));
        } else if !missing.is_empty() {
            col = col.child(h_flex().gap_3().items_center().p_3().rounded_lg().bg(ui::tint(ui::AMBER, 0.12))
                .child(div().flex_1().text_sm().child(format!("Cloud AI needs {} first.", missing.join(", "))))
                .child(Button::new("to-settings").small().primary().label("Open Settings").on_click(cx.listener(|this, _, _, cx| this.go(Page::Settings, cx)))));
        }
        // 1. the video
        let mut pick = ui::section("1 · Video", "mp4, mov, mkv, webm or m4v; short clips (10–60 s) work best.", cx);
        pick = pick.child(h_flex().gap_3().items_center()
            .child(Button::new("choose").primary().label(if video.is_some() { "Choose another video…" } else { "Choose a video…" })
                .on_click(cx.listener(|this, _, window, cx| {
                    this.pick(true, "Choose a cooking video", window, cx, |t, p, _, cx| { t.new_run.video = Some(p); cx.notify(); })
                })))
            .child(div().text_sm().font_weight(FontWeight::MEDIUM).child(video.as_ref().map(|v| v.file_name().unwrap_or_default().to_string_lossy().to_string()).unwrap_or_else(|| "no video chosen".into()))))
            .child(Input::new(&self.path_box));
        col = col.child(pick);
        // 2. the look
        let layer = |id: &'static str, title: &'static str, items: Vec<(String, String)>, chosen: Option<String>, cx: &mut Context<Self>| {
            v_flex().gap_2().child(ui::label(title, cx)).child(h_flex().gap_2().flex_wrap().children(items.into_iter().map(|(name, label)| {
                let on = chosen.as_deref() == Some(name.as_str());
                Button::new(SharedString::from(format!("{id}-{name}"))).small().outline().selected(on).label(label)
                    .on_click(cx.listener(move |this, _, _, cx| {
                        let slot = match id { "anim" => &mut this.new_run.animation, "dir" => &mut this.new_run.director, _ => &mut this.new_run.finish };
                        *slot = if slot.as_deref() == Some(name.as_str()) { None } else { Some(name.clone()) };
                        cx.notify();
                    }))
            })))
        };
        let cat = self.catalog.clone();
        let mut look = ui::section("2 · Look", "Pick one in each row, or leave a row empty for the default. You can still change it before approving.", cx);
        if cat.animation.is_empty() {
            look = look.child(div().text_sm().text_color(ui::rgb(ui::MUTED)).child("Style presets appear when the engine is running."));
        } else {
            look = look
                .child(layer("anim", "Animation style", cat.animation, self.new_run.animation.clone(), cx))
                .child(layer("dir", "Director's language", cat.director, self.new_run.director.clone(), cx))
                .child(layer("fin", "Film finish", cat.finish, self.new_run.finish.clone(), cx));
        }
        col = col.child(look);
        // 3. output and sound
        let res = self.new_run.resolution;
        let audio = self.new_run.audio;
        col = col.child(ui::section("3 · Output and sound", "", cx)
            .child(h_flex().gap_6()
                .child(v_flex().gap_2().child(ui::label("Resolution", cx)).child(h_flex().gap_2().children(["720p", "1080p"].into_iter().map(|r| {
                    Button::new(SharedString::from(format!("res-{r}"))).small().outline().selected(res == r).label(r)
                        .on_click(cx.listener(move |this, _, _, cx| { this.new_run.resolution = r; cx.notify(); }))
                }))))
                .child(v_flex().gap_2().child(ui::label("Sound", cx)).child(h_flex().gap_2().children([("regenerate", "New foley + voice sounds"), ("keep_voice", "Keep the voice"), ("silent", "Silent")].into_iter().map(|(m, l)| {
                    Button::new(SharedString::from(format!("aud-{m}"))).small().outline().selected(audio == m).label(l)
                        .on_click(cx.listener(move |this, _, _, cx| { this.new_run.audio = m; cx.notify(); }))
                }))))));
        if let Some(n) = self.render_notice() {
            col = col.child(n);
        }
        let ready = video.is_some() && self.busy.is_none() && self.engine.studio().is_some() && missing.is_empty();
        col = col.child(h_flex().justify_end().child(Button::new("start").primary().large().label("Start run").disabled(!ready)
            .on_click(cx.listener(|this, _, window, cx| this.start_new_run(window, cx)))));
        col.into_any_element()
    }

    fn render_settings(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let first = !self.settings.configured;
        let f = &self.form;
        let cloud = f.mode == settings::Mode::Cloud;
        let mut col = v_flex().w_full().gap_4().max_w(px(920.))
            .child(v_flex().gap_1()
                .child(div().text_2xl().font_weight(FontWeight::BOLD).child(if first { "Welcome — set up the AI" } else { "Settings" }))
                .child(div().text_sm().text_color(ui::rgb(ui::MUTED)).child("c2a uses three cloud services: a vision LLM watches your video, fal.ai draws and animates, Fish Audio adds voice sounds. Keys stay on this computer (~/.config/c2a-review, readable only by you).")));
        // mode
        let mode_btn = |id: &'static str, title: &'static str, sub: &'static str, m: settings::Mode, on: bool, cx: &mut Context<Self>| {
            v_flex().id(id).flex_1().min_w_0().gap_1().p_4().rounded_xl().cursor_pointer().border_1()
                .border_color(if on { ui::rgb(ui::ACCENT) } else { ui::rgb(ui::LINE) })
                .bg(if on { ui::tint(ui::ACCENT, 0.10) } else { ui::rgb(ui::CARD) })
                .child(div().font_weight(FontWeight::SEMIBOLD).child(title))
                .child(div().text_sm().text_color(ui::rgb(ui::MUTED)).child(sub))
                .on_click(cx.listener(move |this, _, _, cx| { this.form.mode = m; cx.notify(); }))
        };
        col = col.child(h_flex().w_full().gap_3()
            .child(mode_btn("mode-cloud", "Cloud AI", "Real anime restyle, animation and sound. Needs API keys; you pay the providers per use.", settings::Mode::Cloud, cloud, cx))
            .child(mode_btn("mode-demo", "Demo", "No AI and no keys: instant stand-in results, to learn the app.", settings::Mode::Demo, !cloud, cx)));
        if cloud {
            // vision LLM
            let base_now = f.vlm_base.read(cx).value().to_string();
            let mut vlm = ui::section("Vision LLM", "Watches each key moment and tags the action, motion, camera and mood; also checks for on-screen text. Any OpenAI-compatible API with image input.", cx)
                .child(h_flex().gap_2().flex_wrap().children(settings::VLM_PRESETS.iter().enumerate().map(|(i, p)| {
                    Button::new(SharedString::from(format!("preset-{i}"))).small().outline().selected(base_now == p.base_url).label(p.name)
                        .on_click(cx.listener(move |this, _, window, cx| this.apply_preset(i, window, cx)))
                })));
            let hint = settings::VLM_PRESETS.iter().find(|p| p.base_url == base_now).map(|p| p.key_hint).unwrap_or("your provider's console");
            vlm = vlm.child(h_flex().gap_3()
                    .child(v_flex().flex_1().gap_1().child(ui::label("Base URL", cx)).child(Input::new(&f.vlm_base)))
                    .child(v_flex().w(px(260.)).gap_1().child(ui::label("Model", cx)).child(Input::new(&f.vlm_model))))
                .child(v_flex().gap_1().child(ui::label(&format!("API key · get one at {hint}"), cx)).child(Input::new(&f.vlm_key)));
            col = col.child(vlm);
            // video generation: fal.ai or your own GPU
            let backend = f.video_backend.clone();
            let comfy = backend == "comfyui";
            let mut video = ui::section("Video generation", "Animates each approved key image with Wan 2.2 image-to-video.", cx)
                .child(h_flex().gap_2().children([("fal", "fal.ai (cloud)"), ("comfyui", "ComfyUI on your GPU (e.g. DGX)")].into_iter().map(|(b, l)| {
                    Button::new(SharedString::from(format!("vb-{b}"))).small().outline().selected(backend == b).label(l)
                        .on_click(cx.listener(move |this, _, _, cx| { this.form.video_backend = b.into(); cx.notify(); }))
                })));
            if comfy {
                video = video
                    .child(v_flex().gap_1().child(ui::label("ComfyUI URL (Wan 2.2 installed by scripts/comfyui/setup_wan22.sh)", cx)).child(Input::new(&f.comfy_url)))
                    .child(div().text_xs().text_color(ui::rgb(ui::MUTED)).child("“Save and check” verifies the server and that the Wan 2.2 model files are there."));
            }
            col = col.child(video);
            // fal.ai
            let res = f.video_resolution.clone();
            col = col.child(ui::section(if comfy { "fal.ai — restyle and foley" } else { "fal.ai — restyle, animation, foley" },
                if comfy { "Draws the anime frames (Nano Banana) and makes sound effects (MMAudio). Key: fal.ai/dashboard/keys." }
                else { "Draws the anime frames (Nano Banana), animates them (Wan 2.2) and makes sound effects (MMAudio). Key: fal.ai/dashboard/keys." }, cx)
                .child(v_flex().gap_1().child(ui::label("API key", cx)).child(Input::new(&f.fal_key)))
                .when(!comfy, |d| d.child(v_flex().gap_2().child(ui::label("Video quality (480p is faster and cheaper; the result is upscaled either way)", cx))
                    .child(h_flex().gap_2().children(["480p", "720p"].into_iter().map(|r| {
                        Button::new(SharedString::from(format!("vres-{r}"))).small().outline().selected(res == r).label(r)
                            .on_click(cx.listener(move |this, _, _, cx| { this.form.video_resolution = r.into(); cx.notify(); }))
                    })))))
                .child(h_flex().gap_3()
                    .child(v_flex().flex_1().gap_1().child(ui::label("Image model (advanced)", cx)).child(Input::new(&f.fal_image)))
                    .when(!comfy, |d| d.child(v_flex().flex_1().gap_1().child(ui::label("Video model (advanced)", cx)).child(Input::new(&f.fal_video))))));
            // Fish Audio
            let voice = f.voice_enabled;
            let mut fish = ui::section("Fish Audio — voice sounds", "Small non-verbal sounds (a satisfied sigh, a quiet “mm”) where someone is on screen. Never words. Key: fish.audio → API keys.", cx)
                .child(Switch::new("voice").checked(voice).label("Add voice sounds")
                    .on_click(cx.listener(|this, on: &bool, _, cx| { this.form.voice_enabled = *on; cx.notify(); })));
            if voice {
                fish = fish.child(v_flex().gap_1().child(ui::label("API key", cx)).child(Input::new(&f.fish_key)))
                    .child(h_flex().gap_3()
                        .child(v_flex().w(px(220.)).gap_1().child(ui::label("Voice model", cx)).child(Input::new(&f.voice_model)))
                        .child(v_flex().flex_1().gap_1().child(ui::label("Voice (reference id, optional)", cx)).child(Input::new(&f.voice_ref))));
            }
            col = col.child(fish);
        }
        // engine and storage
        let c2a = self.c2a.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| "not found".into());
        col = col.child(ui::section("Engine and storage", "", cx)
            .child(h_flex().gap_3().items_center()
                .child(v_flex().flex_1().min_w_0().gap_1().child(ui::label("c2a engine", cx)).child(div().text_sm().truncate().child(c2a)))
                .child(Button::new("locate").small().outline().label("Locate c2a…").on_click(cx.listener(|this, _, window, cx| {
                    this.pick(true, "Choose the c2a program", window, cx, |t, p, _, cx| t.set_c2a(p, cx))
                }))))
            .child(h_flex().gap_3().items_center()
                .child(v_flex().flex_1().min_w_0().gap_1().child(ui::label("Runs folder", cx)).child(div().text_sm().truncate().child(self.workdir.display().to_string())))
                .child(Button::new("folder2").small().outline().label("Change…").on_click(cx.listener(|this, _, window, cx| {
                    this.pick(false, "Choose the runs folder", window, cx, |t, p, _, cx| t.set_workdir(p, cx))
                })))));
        // save + checks
        let checking = self.form.checking;
        col = col.child(h_flex().gap_3().items_center().justify_end()
            .when(checking, |d| d.child(div().text_sm().text_color(ui::rgb(ui::MUTED)).child("restarting the engine and checking…")))
            .child(Button::new("save").primary().large().label("Save and check").disabled(checking)
                .on_click(cx.listener(|this, _, _, cx| this.save_settings(cx)))));
        if !self.form.checks.is_empty() {
            let all_ok = self.form.checks.iter().all(|c| c.ok);
            let mut list = ui::section(if all_ok { "All set" } else { "Check results" },
                if all_ok { "Everything works. Start a run from “New run”." } else { "Fix the red items and save again." }, cx);
            for c in self.form.checks.clone() {
                list = list.child(h_flex().gap_3().items_start()
                    .child(div().w(px(18.)).font_weight(FontWeight::BOLD).text_color(ui::rgb(if c.ok { ui::GREEN } else { ui::RED })).child(if c.ok { "✓" } else { "✗" }))
                    .child(v_flex().flex_1().min_w_0().gap_0p5()
                        .child(div().text_sm().font_weight(FontWeight::MEDIUM).child(c.name.clone()))
                        .child(div().text_xs().text_color(ui::rgb(ui::MUTED)).child(c.detail.clone()))
                        .when(!c.ok && !c.fix.is_empty(), |d| d.child(div().text_xs().text_color(ui::rgb(ui::AMBER)).child(c.fix.clone())))));
            }
            if all_ok {
                list = list.child(h_flex().justify_end().child(Button::new("go-new").primary().label("New run").on_click(cx.listener(|this, _, _, cx| this.go(Page::New, cx)))));
            }
            col = col.child(list);
        }
        if let Engine::Off(why) = &self.engine {
            if !why.is_empty() {
                col = col.child(div().p_3().rounded_lg().text_sm().bg(ui::tint(ui::RED, 0.12)).child(format!("Engine: {why}")));
            }
        }
        col.into_any_element()
    }
}

fn shellexpand_home(p: &str) -> String {
    match (p.strip_prefix("~/"), engine::home()) {
        (Some(rest), Some(h)) => h.join(rest).to_string_lossy().to_string(),
        _ => p.to_string(),
    }
}

impl Render for Shell {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let content = match (&self.open, self.page) {
            (Some(run), _) => run.clone().into_any_element(),
            (None, Page::Runs) => {
                let mut c = v_flex().gap_4();
                if let Some(n) = self.render_notice() {
                    c = c.child(n);
                }
                c.child(self.render_runs(cx)).into_any_element()
            }
            (None, Page::New) => self.render_new(cx),
            (None, Page::Settings) => self.render_settings(cx),
        };
        h_flex()
            .size_full()
            .bg(cx.theme().background)
            .text_color(cx.theme().foreground)
            .child(self.render_sidebar(cx))
            .child(div().id("main").flex_1().h_full().min_w_0().overflow_y_scroll().p_6().child(content))
    }
}

struct Args {
    workdir: Option<PathBuf>,
    run_dir: Option<PathBuf>,
    c2a: Option<PathBuf>,
    studio: Option<Studio>,
}

fn arg(name: &str) -> Option<String> {
    let args: Vec<String> = std::env::args().collect();
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned())
}

fn main() {
    if std::env::args().any(|a| a == "--help" || a == "-h") {
        println!("usage: c2a-review [--workdir <runs folder>] [--run-dir <run folder>] [--c2a <path to c2a>]\n                  [--studio <url> --token <token>]\nWith no arguments it starts the c2a engine itself; AI providers and keys are set in Settings.");
        return;
    }
    let args = Args {
        workdir: arg("--workdir").map(PathBuf::from),
        run_dir: arg("--run-dir").map(PathBuf::from),
        c2a: arg("--c2a").map(PathBuf::from),
        studio: arg("--studio").map(|base| Studio::new(&base, &arg("--token").unwrap_or_default())),
    };
    gpui_kit::application().run(move |cx| {
        gpui_kit::init(cx);
        Theme::change(ThemeMode::Dark, None, cx);
        cx.on_app_quit(|_| async { engine::stop() }).detach();
        cx.on_window_closed(|cx, _| {
            if cx.windows().is_empty() {
                engine::stop();
                cx.quit();
            }
        })
        .detach();
        let options = WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(Bounds::centered(None, size(px(1280.), px(800.)), cx))),
            titlebar: Some(TitlebarOptions { title: Some("c2a Review".into()), ..Default::default() }),
            app_id: Some("c2a-review".into()),
            ..Default::default()
        };
        gpui_kit::open_window(options, cx, move |window, cx| cx.new(|cx| Shell::new(args, window, cx)))
            .expect("failed to open window");
    });
}
