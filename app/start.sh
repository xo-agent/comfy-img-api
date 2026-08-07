#!/bin/bash
# Boot the full stack: ComfyUI + FastAPI + cloudflared + keepalive
# Runs as sandbox user via setsid from the watchdog.
exec > /tmp/boot.log 2>&1
cd /home/user || exit 1

echo "=== boot $(date) ==="

# 1) wait for watchdog-injected secrets (CF token, E2B key, own id)
for i in $(seq 1 60); do
  [ -s .cf_token ] && [ -s .e2b_id ] && break
  sleep 5
done
echo "secrets present: cf_token=$(test -s .cf_token && echo yes || echo no) e2b_id=$(test -s .e2b_id && echo yes || echo no) e2b_key=$(test -s .e2b_key && echo yes || echo no)"

# 2) clean stale runtime logs (template snapshots may carry them -> false positives)
rm -f /tmp/cf.log /tmp/boot.log /tmp/app.log /tmp/keepalive.log /tmp/comfy.log
# 3) pull latest app code (public repo, no auth)
if [ -d /home/user/comfy-img-api/.git ]; then
  git -C /home/user/comfy-img-api pull --ff-only -q 2>/dev/null && echo "app updated" || echo "app pull failed (using baked copy)"
fi

# 4) start FastAPI FIRST (serves site + API on :80 — the CF tunnel route target)
cd /home/user/app
nohup sudo -n /home/user/ComfyUI/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 80 > /tmp/app.log 2>&1 &
echo "uvicorn started on :80"

# 5) start cloudflared tunnel -> ox-img.oxu.indevs.in
nohup /home/user/cloudflared tunnel --no-autoupdate run --token "$(cat /home/user/.cf_token)" > /tmp/cf.log 2>&1 &
echo "cloudflared started"

# 6) anti-idle keepalive (self E2B API ping)
setsid /home/user/ComfyUI/venv/bin/python /home/user/app/keepalive.py > /tmp/keepalive.log 2>&1 &
echo "keepalive started"

# 7) ComfyUI LAST (warms in background; site+API are up long before it's ready)
NPROC=$(nproc)
cd /home/user/ComfyUI
nohup ./venv/bin/python main.py --cpu --cpu-threads "$NPROC" --port 8188 > /tmp/comfy.log 2>&1 &
echo "comfy started (threads=$NPROC)"
# 8) warm the default model: one tiny 1-step job right after ComfyUI is up,
#    so the first user request doesn't pay the model-load penalty
(
  for i in $(seq 1 60); do
    curl -s http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && break
    sleep 2
  done
  python3 - <<'PYEOF'
import json, urllib.request, time
wf = {
  "3": {"class_type":"KSampler","inputs":{"seed":1,"steps":1,"cfg":1.0,"sampler_name":"euler","scheduler":"normal","denoise":1.0,"model":["4",0],"positive":["6",0],"negative":["7",0],"latent_image":["5",0]}},
  "4": {"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"sd_turbo.safetensors"}},
  "5": {"class_type":"EmptyLatentImage","inputs":{"width":64,"height":64,"batch_size":1}},
  "6": {"class_type":"CLIPTextEncode","inputs":{"text":"warmup","clip":["4",1]}},
  "7": {"class_type":"CLIPTextEncode","inputs":{"text":"","clip":["4",1]}},
  "8": {"class_type":"VAEDecode","inputs":{"samples":["3",0],"vae":["4",2]}},
  "9": {"class_type":"SaveImage","inputs":{"filename_prefix":"warmup","images":["8",0]}},
}
req = urllib.request.Request("http://127.0.0.1:8188/prompt", data=json.dumps({"prompt":wf,"client_id":"warmup"}).encode(), headers={"Content-Type":"application/json"})
urllib.request.urlopen(req, timeout=30)
print("warmup job submitted")
PYEOF
) > /tmp/warmup.log 2>&1 &

echo "=== boot complete $(date) ==="
