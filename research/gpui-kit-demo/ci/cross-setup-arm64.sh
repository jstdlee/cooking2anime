#!/bin/bash
# Prepares an Ubuntu 24.04 x86_64 machine to cross-build c2a-review for linux arm64:
# the arm64 C toolchain and the arm64 system libraries GPUI links against (run as root;
# then, as the build user: rustup target add aarch64-unknown-linux-gnu).
set -euo pipefail
SRC=/etc/apt/sources.list.d/ubuntu.sources
# Existing mirrors serve amd64 only; arm64 packages come from ports.ubuntu.com.
if ! grep -q "^Architectures:" "$SRC"; then
  sed -i 's/^Types: deb$/Types: deb\nArchitectures: amd64/' "$SRC"
fi
cat > /etc/apt/sources.list.d/ubuntu-ports-arm64.sources <<'SOURCES'
Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: noble noble-updates noble-security
Components: main universe
Architectures: arm64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
SOURCES
dpkg --add-architecture arm64
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  gcc-aarch64-linux-gnu g++-aarch64-linux-gnu libc6-dev-arm64-cross pkg-config \
  libxkbcommon-dev:arm64 libxkbcommon-x11-dev:arm64 libxcb1-dev:arm64 libasound2-dev:arm64 \
  libfontconfig-dev:arm64 libfreetype-dev:arm64 libwayland-dev:arm64 libvulkan-dev:arm64 >/dev/null
