# ADR-006: tldraw SDK for the Studio canvas

Status: accepted (v0.2.0) · Replaces: the GoDraw board integration of v0.1.0

## Context
The Studio must put every action on one canvas: check key images, edit tags, pick
style layers, approve a sample, play clips inline, re-run, open the output. GoDraw is
a general whiteboard; it cannot host our own interactive panels without forking.

## Findings (spike S6)
- **tldraw 5.6.0** matches the schema version the engine already pinned. Custom shapes
  are React components (`BaseBoxShapeUtil` + `HTMLContainer`), so checkboxes, selects,
  a `<video>` player and buttons work on the canvas. Interactive elements stop pointer
  propagation so the canvas does not treat clicks as selection.
- **License behaviour (read from `LicenseManager`):** pages served over plain `http://`,
  including LAN addresses, run in development mode. The canvas is fully functional,
  no tracking ping is sent, and a small "Get a license for production" note is shown.
  `https://` on a non-loopback host counts as production and needs a key. The Studio
  reads one from `C2A_TLDRAW_LICENSE` and injects it into the page.
- **Offline:** by default tldraw loads fonts, icons and translations from its CDN.
  `@tldraw/assets/imports.vite` bundles them, so the Studio works on an offline GPU box
  (about 4.9 MB of static files).
- tldraw does not render shapes outside the viewport, so the toolbar has buttons that
  move the camera to each panel.

## Decision
- One custom shape type, `c2a-panel`, in five kinds (keys, style, samples, clips,
  output). The engine sends a **view model** over WebSocket and the frontend renders
  it inside the panels. The engine does not write tldraw records (v1 did), so it never
  races the editor, and the person's own drawings and panel layout belong entirely to
  tldraw. The layout persists in the browser (`persistenceKey`) and is snapshotted to
  `runs/<id>/board.tldr` for outside viewers.
- Every action is a typed command handled by `c2a.app`, the same functions the CLI
  calls.
