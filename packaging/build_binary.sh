#!/usr/bin/env bash
# Build the self-contained `c2a` binary for the current machine and smoke-test it.
#   packaging/build_binary.sh <out-dir>
# Produces <out-dir>/c2a-<version>-linux-<arch>.tar.gz (+ .sha256).
# Runtime dependency on the target: ffmpeg/ffprobe on PATH.
set -euo pipefail

OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="$(uname -m)"
case "$ARCH" in aarch64|arm64) ARCH=arm64 ;; x86_64|amd64) ARCH=x86_64 ;; esac
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 -m venv "$WORK/venv"
# shellcheck disable=SC1091
. "$WORK/venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet "$ROOT" "pyinstaller>=6.10"
VERSION="$(python -c 'import c2a; print(c2a.__version__)')"

cd "$ROOT"
pyinstaller packaging/c2a.spec --noconfirm --log-level WARN \
  --distpath "$WORK/dist" --workpath "$WORK/build"
BIN="$WORK/dist/c2a/c2a"

echo "== smoke test ($(uname -m))"
"$BIN" --version
"$BIN" doctor --stack fake
python "$ROOT/tests/fixtures/make_fixtures.py" "$WORK/fx" >/dev/null
"$BIN" run "$WORK/fx/vertical.mp4" --stack fake --workdir "$WORK/runs" \
  --set style.preview.sample_keys=1 --set style.preview.seeds_per_key=1 \
  --set ingest.shot_detector=ffmpeg_scene
RUN="$(ls -d "$WORK"/runs/*/)"
"$BIN" approve "$RUN" --pick 0:0
"$BIN" status "$RUN" --json | python -c 'import json,sys; assert json.load(sys.stdin)["state"]=="done"'
test -s "$RUN/out/vertical_720p.mp4"

echo "== smoke test: studio"
test -f "$WORK/dist/c2a/_internal/c2a/studio/static/index.html" \
  || { echo "Studio frontend missing: build studio/ first"; exit 1; }
C2A_STUDIO_TOKEN=smoke "$BIN" studio --stack fake --no-browser --port 8791 \
  --workdir "$WORK/runs" > "$WORK/studio.log" 2>&1 &
STUDIO=$!
python - <<'PY'
import time, urllib.request
for _ in range(60):
    try:
        page = urllib.request.urlopen("http://127.0.0.1:8791/?token=smoke", timeout=2).read()
        runs = urllib.request.urlopen("http://127.0.0.1:8791/api/runs?token=smoke").read()
        assert b'id="root"' in page and b'"runs"' in runs
        print("studio ok")
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("studio did not start")
PY
kill $STUDIO

NAME="c2a-$VERSION-linux-$ARCH"
mv "$WORK/dist/c2a" "$WORK/$NAME"
cp "$ROOT/README.md" "$ROOT/configs/default.yaml" "$WORK/$NAME/"
tar -C "$WORK" -czf "$OUT/$NAME.tar.gz" "$NAME"
(cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "== built $OUT/$NAME.tar.gz"
