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

# 2) pull latest app code (public repo, no auth)
if [ -d /home/user/comfy-img-api/.git ]; then
  git -C /home/user/comfy-img-api pull --ff-only -q 2>/dev/null && echo "app updated" || echo "app pull failed (using baked copy)"
fi

# 3) start ComfyUI (CPU)
cd /home/user/ComfyUI
nohup ./venv/bin/python main.py --cpu --port 8188 > /tmp/comfy.log 2>&1 &
for i in $(seq 1 120); do
  curl -s http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && { echo "comfy up after $((i*2))s"; break; }
  sleep 2
done

# 4) start FastAPI (serves site + API on :80 — the CF tunnel route target)
cd /home/user/app
nohup sudo -n /home/user/ComfyUI/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 80 > /tmp/app.log 2>&1 &
echo "uvicorn started on :80"

# 5) start cloudflared tunnel -> ox-img.oxu.indevs.in
nohup /home/user/cloudflared tunnel --no-autoupdate run --token "$(cat /home/user/.cf_token)" > /tmp/cf.log 2>&1 &
echo "cloudflared started"

# 6) anti-idle keepalive (self E2B API ping)
setsid /home/user/ComfyUI/venv/bin/python /home/user/app/keepalive.py > /tmp/keepalive.log 2>&1 &
echo "keepalive started"

echo "=== boot complete $(date) ==="
