//! The app's look: a dark palette with a warm accent, cards, state chips and the stage
//! stepper. Colors are plain 0xRRGGBB so they read the same in code and in a design tool.

use gpui_kit::component::{ActiveTheme as _, h_flex};
use gpui_kit::*;

pub const ACCENT: u32 = 0xff7a59; // warm "kitchen" orange
pub const ACCENT_2: u32 = 0xf472b6; // anime pink
pub const CARD: u32 = 0x17171d;
pub const CARD_HOVER: u32 = 0x1f1f27;
pub const LINE: u32 = 0x2a2a35;
pub const SIDEBAR: u32 = 0x101015;
pub const BLUE: u32 = 0x60a5fa;
pub const AMBER: u32 = 0xfbbf24;
pub const GREEN: u32 = 0x34d399;
pub const RED: u32 = 0xf87171;
pub const MUTED: u32 = 0x8b8b9a;

pub fn rgb(c: u32) -> Hsla {
    gpui_kit::rgb(c).into()
}

/// `c` at `alpha` opacity (for soft backgrounds behind colored text).
pub fn tint(c: u32, alpha: f32) -> Hsla {
    let mut h = rgb(c);
    h.a = alpha;
    h
}

/// The pipeline as the person sees it: (states covered, title).
pub const STAGES: &[(&[&str], &str)] = &[
    (&["created", "ingesting"], "Prepare"),
    (&["finding_keys"], "Key images"),
    (&["tagging"], "AI tags"),
    (&["previewing"], "Style samples"),
    (&["awaiting_approval"], "Your approval"),
    (&["restyling"], "Restyle"),
    (&["generating"], "Animate"),
    (&["post"], "Polish"),
    (&["audio"], "Sound"),
    (&["exporting", "done"], "Export"),
];

pub fn stage_title(state: &str) -> &'static str {
    STAGES.iter().find(|(s, _)| s.contains(&state)).map(|(_, t)| *t).unwrap_or("Working")
}

/// (label, color) of a run state for chips and cards.
pub fn state_chip(state: &str) -> (&'static str, u32) {
    match state {
        "done" => ("Done", GREEN),
        "failed" => ("Failed", RED),
        "awaiting_approval" => ("Needs your approval", AMBER),
        "" | "created" => ("New", MUTED),
        _ => ("Working…", BLUE),
    }
}

pub fn chip(label: impl Into<SharedString>, color: u32) -> Div {
    div()
        .px_2()
        .py(px(2.))
        .rounded_full()
        .text_xs()
        .font_weight(FontWeight::MEDIUM)
        .text_color(rgb(color))
        .bg(tint(color, 0.14))
        .child(label.into())
}

/// Ten steps from "Prepare" to "Export"; done ones filled, the current one highlighted
/// (red when the run failed there).
pub fn stepper(state: &str, cx: &App) -> Div {
    let failed = state == "failed";
    let current = STAGES.iter().position(|(s, _)| s.contains(&state));
    let done_all = state == "done";
    h_flex().gap_1().flex_wrap().children(STAGES.iter().enumerate().map(|(i, (_, title))| {
        let (fg, bg) = match current {
            _ if done_all => (GREEN, tint(GREEN, 0.12)),
            Some(c) if i < c => (GREEN, tint(GREEN, 0.12)),
            Some(c) if i == c && state == "awaiting_approval" => (AMBER, tint(AMBER, 0.18)),
            Some(c) if i == c => (BLUE, tint(BLUE, 0.18)),
            _ => (MUTED, cx.theme().secondary),
        };
        let fg = if failed { MUTED } else { fg };
        div().px_2().py_1().rounded_md().text_xs().text_color(rgb(fg)).bg(bg)
            .child(format!("{} {}", i + 1, title))
    }))
}

/// A titled card section (settings, new run).
pub fn section(title: &str, hint: &str, cx: &App) -> Div {
    div()
        .flex()
        .flex_col()
        .gap_3()
        .p_4()
        .rounded_xl()
        .bg(rgb(CARD))
        .border_1()
        .border_color(rgb(LINE))
        .child(
            div().flex().flex_col().gap_1()
                .child(div().font_weight(FontWeight::SEMIBOLD).child(title.to_string()))
                .when(!hint.is_empty(), |d| d.child(div().text_sm().text_color(cx.theme().muted_foreground).child(hint.to_string()))),
        )
}

pub fn label(text: &str, cx: &App) -> Div {
    div().text_xs().font_weight(FontWeight::MEDIUM).text_color(cx.theme().muted_foreground).child(text.to_string())
}

use gpui_kit::prelude::FluentBuilder as _;
