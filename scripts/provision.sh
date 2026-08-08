#!/bin/bash
# Template provisioner: run INSIDE an E2B sandbox (user=user, sudo passwordless).
# Installs ComfyUI (CPU) + SD1.5 + FastAPI deps + cloudflared + app clone.
set -e
exec > /tmp/provision.log 2>&1
echo "=== $(date) PROVISION START ==="
sudo -n apt-get update -qq 2>&1 | tail -1
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git wget curl python3-venv >/dev/null 2>&1
echo "apt done"

cd /home/user
[ -d ComfyUI ] || git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
echo "torch done"
./venv/bin/pip install -q -r requirements.txt
./venv/bin/pip install -q fastapi "uvicorn[standard]" e2b
echo "deps done"

mkdir -p models/checkpoints
if [ ! -f models/checkpoints/v1-5-pruned-emaonly.safetensors ]; then
  echo "downloading SD1.5 (4GB)..."
  wget -q -O models/checkpoints/v1-5-pruned-emaonly.safetensors \
    "https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors"
fi
if [ ! -f models/checkpoints/sd_turbo.safetensors ]; then
  echo "downloading SD-Turbo (2.4GB)..."
  wget -q -O models/checkpoints/sd_turbo.safetensors \
    "https://huggingface.co/stabilityai/sd-turbo/resolve/main/sd_turbo.safetensors"
fi
mkdir -p models/upscale_models
if [ ! -f models/upscale_models/RealESRGAN_x2plus.pth ]; then
  echo "downloading RealESRGAN_x2plus (66MB)..."
  wget -q -O models/upscale_models/RealESRGAN_x2plus.pth \
    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x2plus.pth"
fi
echo "model done"

# cloudflared binary
if [ ! -x /home/user/cloudflared ]; then
  wget -q -O /home/user/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x /home/user/cloudflared
fi
/home/user/cloudflared --version | head -1

# app repo — clone into /home/user/comfy-img-api, symlink /home/user/app -> app/
if [ ! -d /home/user/comfy-img-api/.git ]; then
  git clone -q -b main https://github.com/xo-agent/comfy-img-api /home/user/comfy-img-api
fi
ln -sfn /home/user/comfy-img-api/app /home/user/app
ls /home/user/app

# smoke: comfy import
./venv/bin/python -c "import torch; print('torch', torch.__version__, 'threads', torch.get_num_threads())"
echo "=== $(date) PROVISION DONE ==="
