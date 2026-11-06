//! Canvas stress benchmark (GPUI Kit side). The same script runs on tldraw in
//! `bench/web/` so the two stacks can be compared frame by frame.
//!
//!   canvas_bench --images <dir with t0.png..> [--video clip.mp4] --n 5000 [--cull]
//!
//! Script (seconds after a 3 s warm-up), one phase every 4 s:
//!   idle → pan → zoom (down to 0.08×, most cards on screen) → drag 200 selected cards →
//!   duplicate the 200 cards 8 times (+1600 cards).
//! A 1080p H.264 video plays in an overlay the whole time. Output: one JSON line.

#[path = "../video/mod.rs"]
mod video;

use std::f32::consts::TAU;
use std::path::PathBuf;
use std::time::Instant;

use gpui_kit::*;
use video::{PlayerConfig, Source, VideoPlayer, video_view};

const CELL: (f32, f32) = (240.0, 144.0);
const CARD: (f32, f32) = (220.0, 124.0);
const WARMUP: f32 = 3.0;
const PHASES: [&str; 5] = ["idle", "pan", "zoom", "drag", "duplicate"];
const PHASE_S: f32 = 4.0;
const SEL_COLS: usize = 20;
const SEL_ROWS: usize = 10;

#[derive(Clone, Copy)]
struct Card {
    x: f32,
    y: f32,
    img: usize,
}

struct Bench {
    imgs: Vec<PathBuf>,
    cards: Vec<Card>,
    selection: Vec<usize>,
    dups_done: usize,
    cull: bool,
    video: Option<Entity<VideoPlayer>>,
    t0: Instant,
    load_ms: f64,
    last: Option<Instant>,
    frames: Vec<Vec<f32>>,
    cpu_at: Vec<Option<Vec<(String, f64)>>>,
    finished: bool,
}

/// The camera at script time `t`: (world centre x, world centre y, zoom).
fn camera(t: f32) -> (f32, f32, f32) {
    let p = (t / PHASE_S) as usize;
    let u = (t % PHASE_S) / PHASE_S;
    match p {
        1 => (640.0 + 1500.0 * (TAU * u).sin(), 400.0 + 300.0 * (1.0 - (TAU * u).cos()), 1.0),
        2 => (640.0, 400.0, (0.08f32.ln() * 0.5 * (1.0 - (TAU * u).cos())).exp()),
        3 | 4 => (1600.0, 1000.0, 0.4),
        _ => (640.0, 400.0, 1.0),
    }
}

fn drag_offset(t: f32) -> (f32, f32) {
    if (t / PHASE_S) as usize != 3 {
        return (0.0, 0.0);
    }
    let u = (t % PHASE_S) / 2.0;
    (300.0 * (TAU * u).sin(), 150.0 * (TAU * u).sin())
}

impl Bench {
    fn new(imgs: Vec<PathBuf>, video_path: PathBuf, n: usize, cull: bool, cx: &mut Context<Self>) -> Self {
        let t = Instant::now();
        let cols = ((n as f32).sqrt() * 1.5).ceil() as usize;
        let cards: Vec<Card> = (0..n)
            .map(|i| Card { x: (i % cols) as f32 * CELL.0, y: (i / cols) as f32 * CELL.1, img: i % imgs.len() })
            .collect();
        let selection = (0..SEL_ROWS).flat_map(|r| (0..SEL_COLS).map(move |c| r * cols + c)).filter(|i| *i < n).collect();
        let video = (!video_path.as_os_str().is_empty()).then(|| {
            let src = Source::parse(&video_path.to_string_lossy()).expect("valid path");
            cx.new(|cx| VideoPlayer::new(src, PlayerConfig { autoplay: true, ..Default::default() }, cx))
        });
        Self {
            imgs, cards, selection, dups_done: 0, cull, video,
            t0: Instant::now(), load_ms: t.elapsed().as_secs_f64() * 1e3, last: None,
            frames: vec![Vec::new(); PHASES.len()], cpu_at: vec![None; PHASES.len() + 1], finished: false,
        }
    }

    fn report(&self) -> String {
        let mut out = format!(
            "{{\"stack\":\"gpui-kit\",\"cull\":{},\"cards_end\":{},\"load_ms\":{:.0},\"rss_mb\":{:.0},\"phases\":{{",
            self.cull, self.cards.len(), self.load_ms, rss_mb()
        );
        for (i, name) in PHASES.iter().enumerate() {
            let mut v = self.frames[i].clone();
            v.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let q = |p: f32| v.get(((v.len() as f32 - 1.0) * p).round() as usize).copied().unwrap_or(0.0);
            let jank = v.iter().filter(|x| **x > 33.4).count();
            // CPU cores used per group of threads (main = UI thread: render, layout, scene)
            let (mut main, mut raster, mut decode, mut other) = (0.0, 0.0, 0.0, 0.0);
            if let (Some(a), Some(b)) = (&self.cpu_at[i], &self.cpu_at[i + 1]) {
                for (name, end) in b {
                    let start = a.iter().find(|x| &x.0 == name).map(|x| x.1).unwrap_or(0.0);
                    let d = (end - start) / PHASE_S as f64;
                    match name.as_str() {
                        "main" => main += d,
                        n if n.starts_with("llvmpipe") || n.starts_with("lp") => raster += d,
                        n if n.starts_with("child:") || n.contains("dec") || n.contains("alhazen") => decode += d,
                        _ => other += d,
                    }
                }
            }
            let main_ms = if v.is_empty() { 0.0 } else { main * PHASE_S as f64 * 1e3 / v.len() as f64 };
            let cpu = main + raster + decode + other;
            out += &format!(
                "{}\"{name}\":{{\"fps\":{:.1},\"p50\":{:.1},\"p95\":{:.1},\"p99\":{:.1},\"max\":{:.1},\"jank\":{jank},\"cpu_cores\":{cpu:.2},\"main_cores\":{main:.2},\"main_ms_per_frame\":{main_ms:.1},\"raster_cores\":{raster:.2},\"decode_cores\":{decode:.2},\"other_cores\":{other:.2}}}",
                if i > 0 { "," } else { "" },
                v.len() as f32 / PHASE_S, q(0.5), q(0.95), q(0.99), q(1.0)
            );
        }
        out + "}}"
    }
}

/// (name, cpu seconds) of a /proc stat file: user + system time.
fn stat_cpu(path: &std::path::Path) -> Option<(String, f64)> {
    let stat = std::fs::read_to_string(path).ok()?;
    let name = stat[stat.find('(')? + 1..stat.rfind(')')?].to_string();
    let f: Vec<f64> = stat[stat.rfind(')')? + 2..].split_whitespace().map(|x| x.parse().unwrap_or(0.0)).collect();
    Some((name, (f.get(11)? + f.get(12)?) / 100.0))
}

/// CPU seconds so far for each thread of this process ("main" = the UI thread), and for
/// each child process (the ffmpeg decoder) as "child:<name>".
fn cpu_by_thread() -> Vec<(String, f64)> {
    let pid = std::process::id().to_string();
    let mut out: Vec<(String, f64)> = Vec::new();
    let mut add = |k: String, v: f64| match out.iter_mut().find(|x| x.0 == k) {
        Some(x) => x.1 += v,
        None => out.push((k, v)),
    };
    for task in std::fs::read_dir("/proc/self/task").into_iter().flatten().flatten() {
        let tid = task.file_name().to_string_lossy().to_string();
        if let Some((name, cpu)) = stat_cpu(&task.path().join("stat")) {
            let key = if tid == pid { "main".to_string() } else { name.trim_end_matches(|c: char| c.is_ascii_digit() || c == '-' || c == ':').to_string() };
            add(key, cpu);
        }
        for child in std::fs::read_to_string(task.path().join("children")).unwrap_or_default().split_whitespace() {
            if let Some((name, cpu)) = stat_cpu(&std::path::Path::new("/proc").join(child).join("stat")) {
                add(format!("child:{name}"), cpu);
            }
        }
    }
    out
}

fn rss_mb() -> f64 {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| s.lines().find(|l| l.starts_with("VmRSS")).map(String::from))
        .and_then(|l| l.split_whitespace().nth(1).and_then(|k| k.parse::<f64>().ok()))
        .map(|kb| kb / 1024.0)
        .unwrap_or(0.0)
}

impl Render for Bench {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        window.request_animation_frame();
        let now = Instant::now();
        let t = now.duration_since(self.t0).as_secs_f32() - WARMUP;
        if let Some(last) = self.last {
            if t >= 0.0 && (t / PHASE_S) < PHASES.len() as f32 {
                self.frames[(t / PHASE_S) as usize].push(now.duration_since(last).as_secs_f32() * 1e3);
            }
        }
        self.last = Some(now);
        // process CPU (all threads: UI, layout, decoder, software rasterizer) at each phase start
        if t >= 0.0 {
            let p = ((t / PHASE_S) as usize).min(PHASES.len());
            if self.cpu_at[p].is_none() {
                self.cpu_at[p] = Some(cpu_by_thread());
            }
        }
        if t >= PHASE_S * PHASES.len() as f32 && !self.finished {
            self.finished = true;
            println!("{}", self.report());
            cx.defer(|cx| cx.quit());
        }
        // duplicate: at 0.5 s steps in the last phase, copy the 200 selected cards
        if (t / PHASE_S) as usize == 4 {
            let due = (((t - 4.0 * PHASE_S) / 0.5) as usize + 1).min(8);
            while self.dups_done < due {
                self.dups_done += 1;
                let d = 20.0 * self.dups_done as f32;
                let copies: Vec<Card> = self.selection.iter().map(|&i| Card { x: self.cards[i].x + d, y: self.cards[i].y + d, ..self.cards[i] }).collect();
                self.cards.extend(copies);
            }
        }

        let (cxw, cyw, z) = camera(t.max(0.0));
        let (dx, dy) = drag_offset(t.max(0.0));
        let vp = window.viewport_size();
        let (vw, vh) = (f32::from(vp.width), f32::from(vp.height));
        let n_orig = self.cards.len() - self.dups_done * self.selection.len();
        let selected = |i: usize, sel: &[usize]| i < n_orig && sel.binary_search(&i).is_ok();
        let mut sel_sorted = self.selection.clone();
        sel_sorted.sort_unstable();

        let mut children = Vec::with_capacity(self.cards.len());
        for (i, c) in self.cards.iter().enumerate() {
            let sel = selected(i, &sel_sorted);
            let (wx, wy) = if sel { (c.x + dx, c.y + dy) } else { (c.x, c.y) };
            let (sx, sy) = ((wx - cxw) * z + vw / 2.0, (wy - cyw) * z + vh / 2.0);
            let (w, h) = (CARD.0 * z, CARD.1 * z);
            if self.cull && (sx + w < 0.0 || sy + h < 0.0 || sx > vw || sy > vh) {
                continue;
            }
            children.push(
                div()
                    .absolute()
                    .left(px(sx))
                    .top(px(sy))
                    .w(px(w))
                    .h(px(h))
                    .border_1()
                    .border_color(if sel { rgb(0x2563eb) } else { rgb(0xd4d4d4) })
                    .child(img(self.imgs[c.img].clone()).size_full().object_fit(ObjectFit::Cover)),
            );
        }
        div()
            .size_full()
            .relative()
            .bg(rgb(0xf8f8f8))
            .overflow_hidden()
            .children(children)
            .children(self.video.clone().map(|v| {
                div().absolute().top(px(8.)).right(px(8.)).w(px(480.)).h(px(270.)).bg(rgb(0x000000)).child(video_view(v).size_full())
            }))
    }
}

fn arg(name: &str) -> Option<String> {
    let a: Vec<String> = std::env::args().collect();
    a.iter().position(|x| x == name).and_then(|i| a.get(i + 1).cloned())
}

fn main() {
    let dir = PathBuf::from(arg("--images").expect("--images <dir>"));
    let video_path = PathBuf::from(arg("--video").unwrap_or_default()); // empty: no video
    let n: usize = arg("--n").and_then(|s| s.parse().ok()).unwrap_or(1000);
    let cull = std::env::args().any(|a| a == "--cull");
    if std::env::args().any(|a| a == "--threads") {
        // debug: print thread names once the app is running
        std::thread::spawn(|| {
            std::thread::sleep(std::time::Duration::from_secs(6));
            eprintln!("THREADS {:?}", cpu_by_thread());
        });
    }
    let mut imgs: Vec<PathBuf> = (0..).map(|i| dir.join(format!("t{i}.png"))).take_while(|p| p.exists()).collect();
    imgs.truncate(500);
    assert!(!imgs.is_empty(), "no t<i>.png in {}", dir.display());
    gpui_kit::application().run(move |cx| {
        gpui_kit::init(cx);
        let options = WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(Bounds::new(point(px(0.), px(0.)), size(px(1280.), px(800.))))),
            ..Default::default()
        };
        cx.open_window(options, move |_, cx| cx.new(|cx| Bench::new(imgs, video_path, n, cull, cx))).expect("window");
    });
}
