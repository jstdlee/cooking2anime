#!/bin/bash
# Canvas stress benchmark: GPUI Kit (with and without culling) vs tldraw, same script.
#   bench/run-all.sh <assets dir>    (assets: t0.png..t499.png + clip.mp4; see bench/README.md)
# Needs an X display ($DISPLAY, e.g. Xvfb), a release build of canvas_bench, and the web
# bench built with BENCH_ASSETS=<assets dir> and served by `npm run preview` (port 4179).
set -euo pipefail
A=$(realpath "$1"); HERE=$(cd "$(dirname "$0")" && pwd)
BIN=$HERE/../target/release/canvas_bench
for n in 1000 5000; do
  for cull in "" "--cull"; do
    $BIN --images "$A" --video "$A/clip.mp4" --n $n $cull 2>/dev/null | grep '^{' | sed "s/^{/{\"n\":$n,/"
  done
  (cd "$HERE/web" && node run.cjs $n) | grep '^{' | sed "s/^{/{\"n\":$n,/"
done
