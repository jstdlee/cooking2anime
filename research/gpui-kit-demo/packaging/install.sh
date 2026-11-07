#!/bin/sh
# Adds c2a Review to your desktop's app menu (for the current user; no root needed).
#   ./install.sh               add it (run from the unpacked folder; keep the folder there)
#   ./install.sh --uninstall   remove it
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/scalable/apps"
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$APPS/c2a-review.desktop" "$ICONS/c2a-review.svg"
  update-desktop-database "$APPS" 2>/dev/null || true
  echo "Removed c2a Review from the app menu."
  exit 0
fi
mkdir -p "$APPS" "$ICONS"
cp "$HERE/c2a-review.svg" "$ICONS/c2a-review.svg"
cat > "$APPS/c2a-review.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=c2a Review
GenericName=Cooking video to anime
Comment=Turn cooking videos into anime: runs, video review, key images, style and AI operations
Exec="$HERE/c2a-review"
Icon=c2a-review
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=c2a-review
DESKTOP
chmod +x "$APPS/c2a-review.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
echo "Added c2a Review to your app menu (it runs $HERE/c2a-review)."
