#!/usr/bin/env bash
# ComfyUI + Wan 2.2 image-to-video on your own NVIDIA machine (DGX Spark, DGX, a workstation),
# for c2a's generate.backend = comfyui. Run as your normal user (no root needed except for
# apt packages, which it only lists). Re-running is safe: it updates and resumes downloads.
#
#   scripts/comfyui/setup_wan22.sh [--dir ~/comfyui] [--listen 127.0.0.1] [--port 8188] [--no-service]
#
# Then in c2a Review: Settings → Video generation → ComfyUI, URL http://<host>:8188.
# Needs ~40 GB of disk for the models and a GPU with ≥ 24 GB (or unified memory, as on DGX Spark).
set -euo pipefail

DIR="$HOME/comfyui"; LISTEN=127.0.0.1; PORT=8188; SERVICE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --listen) LISTEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    -h|--help) sed -n 2,11p "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing: $1 (sudo apt-get install -y $2)"; exit 1; }; }
need git git; need python3 python3; need curl curl; need nvidia-smi "the NVIDIA driver"
python3 -c 'import venv' 2>/dev/null || { echo "missing: python3-venv (sudo apt-get install -y python3-venv)"; exit 1; }

ARCH=$(uname -m)
CAP=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d ' ')
GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
# PyTorch wheels: arm64 (DGX Spark, GB10) and new GPUs → CUDA 13.0; Volta (V100, sm_70,
# e.g. DGX-1/DGX-2) is not in the CUDA 12.8+ wheels → CUDA 12.6.
if [ "${CAP%%.*}" -lt 8 ]; then TORCH_CUDA=cu126; else TORCH_CUDA=cu130; fi
say "GPU: $GPU (compute $CAP), $ARCH → PyTorch $TORCH_CUDA"
if [ "${CAP%%.*}" -lt 8 ]; then
  echo "note: this GPU has no native fp8/bf16; ComfyUI casts the fp8 Wan weights (works, slower)."
fi

say "ComfyUI in $DIR"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$DIR"; fi
cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q torch torchvision torchaudio --index-url "https://download.pytorch.org/whl/$TORCH_CUDA"
pip install -q -r requirements.txt
python - <<'PY'
import torch
assert torch.cuda.is_available(), "PyTorch cannot see the GPU (driver / CUDA mismatch?)"
print("torch", torch.__version__, "·", torch.cuda.get_device_name(0))
PY

say "Wan 2.2 14B image-to-video models (~36 GB; downloads resume)"
HF=https://huggingface.co/Comfy-Org
fetch() {  # fetch <dest dir> <file> <url> [fallback url]
  mkdir -p "$1"
  if [ -s "$1/$2" ] && [ ! -e "$1/$2.part" ]; then echo "have $2"; return; fi
  echo "get  $2"
  if ! curl -fL --retry 5 -C - -o "$1/$2.part" "$3"; then
    [ -n "${4:-}" ] || { echo "download failed: $3"; exit 1; }
    curl -fL --retry 5 -C - -o "$1/$2.part" "$4"
  fi
  mv "$1/$2.part" "$1/$2"
}
R22=$HF/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files
R21=$HF/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files
fetch models/diffusion_models wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors "$R22/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
fetch models/diffusion_models wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors "$R22/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
fetch models/text_encoders umt5_xxl_fp8_e4m3fn_scaled.safetensors "$R22/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" "$R21/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
fetch models/vae wan_2.1_vae.safetensors "$R22/vae/wan_2.1_vae.safetensors" "$R21/vae/wan_2.1_vae.safetensors"

if [ "$SERVICE" = 1 ] && command -v systemctl >/dev/null; then
  say "user service comfyui.service on $LISTEN:$PORT"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/comfyui.service" <<UNIT
[Unit]
Description=ComfyUI (Wan 2.2 for c2a)
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python main.py --listen $LISTEN --port $PORT
Restart=on-failure

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now comfyui.service
  loginctl enable-linger "$USER" 2>/dev/null || true   # keep it running after logout
  for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:$PORT/system_stats" >/dev/null && break; sleep 2; done
  curl -fs "http://127.0.0.1:$PORT/system_stats" >/dev/null && echo "ComfyUI is up: http://$LISTEN:$PORT" \
    || { echo "ComfyUI did not answer; see: journalctl --user -u comfyui -n 50"; exit 1; }
else
  say "start it with: cd $DIR && .venv/bin/python main.py --listen $LISTEN --port $PORT"
fi
echo
echo "c2a: Settings → Video generation → ComfyUI, URL http://$( [ "$LISTEN" = 0.0.0.0 ] && hostname -I | awk '{print $1}' || echo 127.0.0.1 ):$PORT"
echo "     or: c2a doctor --set generate.backend=comfyui --set providers.comfyui.url=http://127.0.0.1:$PORT"
