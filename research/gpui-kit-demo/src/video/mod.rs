//! Video playback for the demo: alhazen-core's media engine (UI-agnostic) drawn by a small
//! GPUI element. Ported from alhazen-gpui 0.7.1 (Apache-2.0); see each file's header.

mod cover;
#[allow(dead_code)] // ported API kept whole for later use
mod element;
#[allow(dead_code)]
mod player;

pub use element::video_view;
pub use player::VideoPlayer;
pub use alhazen_core::{PlayerConfig, Source};

/// Frames are asked for as BGRA (what GPUI's RenderImage holds).
pub fn frame_formats() -> Vec<alhazen_core::FrameFormat> {
    vec![alhazen_core::FrameFormat::Bgra]
}
