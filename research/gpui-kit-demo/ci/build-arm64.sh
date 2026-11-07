#!/bin/bash
# Cross-builds c2a-review and canvas_bench for linux arm64 and packages them with the
# app-menu installer (packaging/install.sh) and icon:
#   ci/build-arm64.sh <version>  →  dist/c2a-review-<version>-linux-arm64.tar.gz (+ .sha256)
# Run ci/cross-setup-arm64.sh first (as root).
set -euo pipefail
VERSION=${1:?version}
cd "$(dirname "$0")/.."
T=aarch64-unknown-linux-gnu
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
export CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc CXX_aarch64_unknown_linux_gnu=aarch64-linux-gnu-g++
export PKG_CONFIG_ALLOW_CROSS=1 PKG_CONFIG_SYSROOT_DIR=/ \
       PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
cargo build --release --locked --target $T --bin c2a-review --bin canvas_bench

NAME=c2a-review-$VERSION-linux-arm64
rm -rf "dist/$NAME" && mkdir -p "dist/$NAME"
cp target/$T/release/c2a-review target/$T/release/canvas_bench "dist/$NAME/"
aarch64-linux-gnu-strip "dist/$NAME/c2a-review" "dist/$NAME/canvas_bench"
cp README.md NOTES.md packaging/install.sh packaging/c2a-review.svg "dist/$NAME/"
cat > "dist/$NAME/INSTALL.txt" <<TXT
c2a Review $VERSION for Linux arm64 (glibc 2.39+: Ubuntu 24.04, DGX OS 7)

1. Runtime packages (Debian/Ubuntu):
     sudo apt-get install -y libxkbcommon-x11-0 libasound2t64 libvulkan1 mesa-vulkan-drivers ffmpeg
   (NVIDIA systems: the NVIDIA driver provides Vulkan; mesa-vulkan-drivers is the CPU fallback.)

2. The c2a engine: unpack the c2a release (c2a-<version>-linux-arm64.tar.gz) next to this
   folder or in your home folder. c2a Review finds it there (or on PATH) and starts it
   itself; "Locate c2a…" in the app points to it anywhere else.

3. Add c2a Review to your app menu (once; keep this folder where it is):
     ./install.sh            (./install.sh --uninstall removes it)
   Then open "c2a Review" from the app menu, or run ./c2a-review.

In the app: "New run from video…" starts a run; click a run to review it (videos, key
images, style samples, AI operations); "← Runs" goes back. Runs live in ~/c2a/runs unless
you choose another folder ("Change folder…"). Settings: ~/.config/c2a-review/settings.json
(e.g. "engine_args": ["--stack", "cloud"] to start the engine with the cloud stack).

Advanced: ./c2a-review --help. canvas_bench is the canvas benchmark (see README.md).

Includes code ported from alhazen-gpui 0.7.1 (Apache-2.0); GPUI Kit and GPUI are Apache-2.0.
TXT
tar -C dist -czf "dist/$NAME.tar.gz" "$NAME"
(cd dist && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
file "dist/$NAME/c2a-review" | grep -q "ARM aarch64"
ls -la dist
