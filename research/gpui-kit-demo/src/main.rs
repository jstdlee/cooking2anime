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

impl Render for RunView {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let title = format!("Run {}", self.run_id);
        v_flex()
            .size_full()
            .bg(cx.theme().background)
            .text_color(cx.theme().foreground)
            .p_3()
            .gap_3()
            .child(
                h_flex()
                    .justify_between()
                    .child(h_flex().gap_2().items_center()
                        .child(Button::new("back").small().ghost().label("← Runs")
                            .on_click(cx.listener(|_, _, _, cx| cx.emit(RunViewEvent::Back))))
                        .child(div().font_weight(FontWeight::SEMIBOLD).child(title)))
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

// ==================================================================== the app shell

/// The engine (`c2a studio`) as the app sees it.
#[derive(Clone)]
enum Engine {
    /// Not running; the reason is shown on the start screen.
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

/// The window: the start screen (runs, new run, engine) or one open run.
struct Shell {
    settings: Settings,
    workdir: PathBuf,
    engine: Engine,
    c2a: Option<PathBuf>,
    runs: Vec<RunInfo>,
    open: Option<Entity<RunView>>,
    /// The last thing that happened (or went wrong), shown under the toolbar.
    notice: String,
    /// "uploading …" while a new run is being created.
    busy: Option<String>,
    path_box: Entity<InputState>,
    _tasks: Vec<Task<()>>,
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
        let path_box = cx.new(|cx| InputState::new(window, cx).placeholder("…or paste a path: a video (new run), a run folder, a runs folder, or the c2a program"));
        cx.subscribe_in(&path_box, window, |this: &mut Self, _, ev: &InputEvent, window, cx| {
            if let InputEvent::PressEnter { .. } = ev {
                this.go_path_box(window, cx);
            }
        })
        .detach();
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
        let mut this = Self {
            settings, workdir, engine: Engine::Off(String::new()), c2a: None, runs: Vec::new(), open: None,
            notice: String::new(), busy: None, path_box, _tasks: tasks,
        };
        match args.studio {
            Some(s) => this.engine = Engine::External(s),
            None => this.start_engine(cx),
        }
        if let Some(dir) = args.run_dir {
            this.open_run(dir, window, cx);
        }
        this.refresh_runs(cx);
        this
    }

    fn refresh_runs(&mut self, cx: &mut Context<Self>) {
        let runs = runs::list(&self.workdir);
        if runs != self.runs {
            self.runs = runs;
            cx.notify();
        }
    }

    // ---------------------------------------------------------------- engine
    fn start_engine(&mut self, cx: &mut Context<Self>) {
        if matches!(self.engine, Engine::External(_)) {
            return;
        }
        self.c2a = engine::find_c2a(self.settings.c2a.as_deref());
        let Some(c2a) = self.c2a.clone() else {
            self.set_engine(Engine::Off("c2a engine not found: unpack the c2a release next to this app or in your home folder, or use “Locate c2a…”".into()), cx);
            return;
        };
        self.set_engine(Engine::Starting, cx);
        let (workdir, extra) = (self.workdir.clone(), self.settings.engine_args.clone());
        cx.spawn(async move |this, cx| {
            let got = cx.background_executor().spawn(async move { engine::start(&c2a, &workdir, &extra) }).await;
            let _ = this.update(cx, |t, cx| match got {
                Ok(studio) => t.set_engine(Engine::Ours(studio), cx),
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

    fn engine_label(&self) -> (String, bool) {
        match &self.engine {
            Engine::Off(why) if why.is_empty() => ("engine: off".into(), false),
            Engine::Off(why) => (format!("engine: off — {why}"), false),
            Engine::Starting => ("engine: starting…".into(), false),
            Engine::Ours(_) => (format!("engine: running ({})", self.c2a.as_ref().map(|p| p.display().to_string()).unwrap_or_default()), true),
            Engine::External(_) => ("engine: connected (--studio)".into(), true),
        }
    }

    // ---------------------------------------------------------------- actions
    fn open_run(&mut self, dir: PathBuf, window: &mut Window, cx: &mut Context<Self>) {
        let studio = self.engine.studio();
        let view = cx.new(|cx| RunView::new(dir, studio, window, cx));
        cx.subscribe(&view, |this: &mut Self, _, ev: &RunViewEvent, cx| match ev {
            RunViewEvent::Back => {
                this.open = None;
                this.notice.clear();
                cx.notify();
            }
        })
        .detach();
        self.open = Some(view);
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

    fn new_run(&mut self, video: PathBuf, window: &mut Window, cx: &mut Context<Self>) {
        let Some(studio) = self.engine.studio() else {
            self.notice = "a new run needs the engine: wait for it to start, or fix it above".into();
            cx.notify();
            return;
        };
        self.busy = Some(format!("uploading {} to the engine…", video.file_name().unwrap_or_default().to_string_lossy()));
        cx.notify();
        cx.spawn_in(window, async move |this, cx| {
            let got = cx.background_executor().spawn(async move { studio.create_run(&video) }).await;
            let _ = this.update_in(cx, |t, window, cx| {
                t.busy = None;
                match got {
                    Ok(id) => {
                        t.notice = format!("started run {id}: the engine prepares key images and style samples");
                        let dir = t.workdir.join(&id);
                        t.open_run(dir, window, cx);
                    }
                    Err(e) => t.notice = format!("new run failed: {e:#}"),
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
                    Ok(Err(e)) => t.notice = format!("no file dialog on this desktop ({e:#}): paste the path in the box below instead"),
                    Err(_) => t.notice = "the file dialog closed unexpectedly: paste the path in the box below instead".into(),
                }
                cx.notify();
            });
        })
        .detach();
    }

    /// The path box: a video starts a run, a run folder opens, a folder becomes the runs
    /// folder, an executable named c2a becomes the engine.
    fn go_path_box(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let raw = self.path_box.read(cx).value().trim().trim_matches(|c| c == '"' || c == '\'').to_string();
        if raw.is_empty() {
            return;
        }
        let p = PathBuf::from(shellexpand_home(&raw));
        if runs::is_video(&p) && p.is_file() {
            self.new_run(p, window, cx);
        } else if p.join("manifest.json").is_file() {
            self.open_run(p, window, cx);
        } else if p.is_dir() {
            self.set_workdir(p, cx);
        } else if p.is_file() && p.file_name().is_some_and(|n| n.to_string_lossy().starts_with("c2a")) {
            self.set_c2a(p, cx);
        } else {
            self.notice = format!("not a video, run folder, folder or c2a program: {}", p.display());
        }
        self.path_box.update(cx, |s, cx| s.set_value("", window, cx));
        cx.notify();
    }

    // ---------------------------------------------------------------- start screen
    fn render_home(&mut self, cx: &mut Context<Self>) -> impl IntoElement {
        let (engine_text, engine_ok) = self.engine_label();
        let engine_ready = self.engine.studio().is_some();
        let toolbar = h_flex()
            .gap_2()
            .flex_wrap()
            .items_center()
            .child(Button::new("new-run").primary().label("New run from video…").disabled(!engine_ready || self.busy.is_some())
                .tooltip("pick a cooking video: the engine finds key images and makes style samples")
                .on_click(cx.listener(|this, _, window, cx| {
                    this.pick(true, "Choose a cooking video", window, cx, |t, p, w, cx| t.new_run(p, w, cx))
                })))
            .child(Button::new("open-run").outline().label("Open run folder…")
                .on_click(cx.listener(|this, _, window, cx| {
                    this.pick(false, "Choose a run folder", window, cx, |t, p, w, cx| {
                        if p.join("manifest.json").is_file() { t.open_run(p, w, cx) } else { t.set_workdir(p, cx) }
                    })
                })))
;
        let folder_row = h_flex()
            .gap_2()
            .items_center()
            .child(div().text_sm().font_weight(FontWeight::SEMIBOLD).child(format!("Runs ({})", self.runs.len())))
            .child(div().flex_1().min_w_0().truncate().text_sm().text_color(cx.theme().muted_foreground)
                .child(format!("in {}", self.workdir.display())))
            .child(Button::new("workdir").small().outline().label("Change folder…")
                .on_click(cx.listener(|this, _, window, cx| {
                    this.pick(false, "Choose the runs folder", window, cx, |t, p, _, cx| t.set_workdir(p, cx))
                })));
        let engine_row = h_flex()
            .gap_2()
            .items_center()
            .child(div().size(px(8.)).rounded_full().bg(if engine_ok { gpui_kit::rgb(0x16a34a) } else { gpui_kit::rgb(0xa3a3a3) }))
            .child(div().text_sm().text_color(cx.theme().muted_foreground).child(engine_text))
            .when(matches!(self.engine, Engine::Off(_)), |row| {
                row.child(Button::new("retry").small().ghost().label("Start engine").on_click(cx.listener(|this, _, _, cx| this.start_engine(cx))))
                    .child(Button::new("locate").small().ghost().label("Locate c2a…").on_click(cx.listener(|this, _, window, cx| {
                        this.pick(true, "Choose the c2a program", window, cx, |t, p, _, cx| t.set_c2a(p, cx))
                    })))
            });
        let list: Vec<AnyElement> = self
            .runs
            .clone()
            .into_iter()
            .map(|r| {
                let dir = r.dir.clone();
                let thumb = match &r.thumb {
                    Some(p) => img(p.clone()).w(px(128.)).h(px(72.)).rounded_md().object_fit(ObjectFit::Cover).into_any_element(),
                    None => div().w(px(128.)).h(px(72.)).rounded_md().bg(cx.theme().muted).into_any_element(),
                };
                h_flex()
                    .id(SharedString::from(format!("run-{}", r.id)))
                    .gap_3()
                    .p_2()
                    .items_center()
                    .rounded_md()
                    .border_1()
                    .border_color(cx.theme().border)
                    .bg(cx.theme().background)
                    .hover(|s| s.bg(cx.theme().muted))
                    .cursor_pointer()
                    .child(thumb)
                    .child(v_flex().flex_1().gap_1()
                        .child(div().font_weight(FontWeight::SEMIBOLD).child(r.source.clone()))
                        .child(div().text_sm().text_color(cx.theme().muted_foreground)
                            .child(if r.created.is_empty() { r.id.clone() } else { format!("{} · run {}", r.created, r.id) })))
                    .child(div().text_sm().px_2().py_1().rounded_md().bg(cx.theme().muted).child(state_label(&r.state)))
                    .on_click(cx.listener(move |this, _, window, cx| this.open_run(dir.clone(), window, cx)))
                    .into_any_element()
            })
            .collect();
        let body = if list.is_empty() {
            div().p_6().text_color(cx.theme().muted_foreground)
                .child("No runs in this folder yet. Start one with “New run from video…”, or choose another runs folder.")
                .into_any_element()
        } else {
            v_flex().id("runs").gap_2().overflow_y_scroll().children(list).into_any_element()
        };
        v_flex()
            .size_full()
            .gap_3()
            .child(h_flex().justify_between().items_center()
                .child(div().text_lg().font_weight(FontWeight::SEMIBOLD).child("c2a Review"))
                .child(engine_row))
            .child(toolbar)
            .when_some(self.busy.clone().or_else(|| (!self.notice.is_empty()).then(|| self.notice.clone())), |c, msg| {
                c.child(div().text_sm().p_2().rounded_md().bg(cx.theme().muted).child(msg))
            })
            .child(folder_row)
            .child(div().flex_1().min_h_0().child(body))
            .child(Input::new(&self.path_box))
    }
}

fn state_label(state: &str) -> String {
    match state {
        "" => "new".into(),
        "awaiting_approval" | "gate" => "waiting for your style approval".into(),
        s => s.replace('_', " "),
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
        let content = match &self.open {
            Some(run) => run.clone().into_any_element(),
            None => self.render_home(cx).into_any_element(),
        };
        div().size_full().bg(cx.theme().background).text_color(cx.theme().foreground).p_3().child(content)
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
        println!("usage: c2a-review [--workdir <runs folder>] [--run-dir <run folder>] [--c2a <path to c2a>]\n                  [--studio <url> --token <token>]\nWith no arguments it starts the c2a engine itself and shows the runs in the last runs folder.");
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
