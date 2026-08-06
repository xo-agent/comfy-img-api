#!/usr/bin/env python3
"""Watchdog: keeps the image-gen service at ~100% uptime.

Free-tier E2B hard-caps sandbox lifetime at 1h and reaps idle boxes, so this
runs from GitHub Actions every 45 min (cron) and on every push:

  1. List running sandboxes.
  2. If none: spawn from template `comfy-serve` (or `desktop` fallback + provision).
  3. If the newest box is near expiry (<20 min left): spawn a REPLACEMENT but
     DON'T kill the old one yet — the same CF tunnel token supports multiple
     connectors, so the swap is zero-downtime (old serves until new connects).
  4. Inject secrets (.cf_token, .e2b_key, .e2b_id), pull latest app code, boot.
  5. Wait for public health through the tunnel, then kill the old box.

Secrets (GitHub Actions): E2B_API_KEY, CF_TUNNEL_TOKEN
"""
import json
import os
import sys
import time
import urllib.request

os.environ.setdefault("E2B_API_KEY", os.environ.get("E2B_API_KEY", ""))
from e2b import Sandbox  # noqa: E402

TEMPLATE = os.environ.get("E2B_TEMPLATE", "comfy-serve")
FALLBACK = os.environ.get("E2B_FALLBACK_TEMPLATE", "desktop")
SITE = os.environ.get("SITE_URL", "https://ox-img.oxu.indevs.in")
REPLACE_BEFORE_S = int(os.environ.get("REPLACE_BEFORE_S", "1200"))  # 20 min
HEALTH_WAIT_S = int(os.environ.get("HEALTH_WAIT_S", "600"))


def log(msg):
    print(f"[watchdog {time.strftime('%H:%M:%S')}] {msg}", flush=True)


UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
}


def public_ok(timeout=15):
    try:
        req = urllib.request.Request(SITE + "/api/health", headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def running_boxes():
    boxes = []
    try:
        pag = Sandbox.list()
        items = list(pag.next_items())
        boxes.extend(items)
        while pag.has_next:
            boxes.extend(pag.next_items())
    except Exception as e:
        log(f"Sandbox.list failed: {e}")
    out = []
    for b in boxes:
        try:
            out.append({"id": b.sandbox_id, "state": b.state, "template": getattr(b, "template_id", ""), "info": b.get_info()})
        except Exception as e:
            log(f"info failed for {b.sandbox_id}: {e}")
    return [b for b in out if b["state"] == "running"]


def age_info(b):
    d = b["info"]
    end = d.get("end_at") if isinstance(d, dict) else getattr(d, "end_at", None)
    if not end:
        return None
    try:
        end_ts = end.timestamp() if hasattr(end, "timestamp") else None
        if end_ts is None and isinstance(end, str):
            end_ts = time.mktime(time.strptime(end.split("+")[0], "%Y-%m-%d %H:%M:%S"))
        return end_ts - time.time()
    except Exception as e:
        log(f"end_at parse failed: {e}")
        return None


def boot(box):
    """Inject secrets, pull code, start stack."""
    sb = Sandbox.connect(box["id"])
    sid = sb.sandbox_id
    log(f"booting {sid}")
    tok = os.environ["CF_TUNNEL_TOKEN"]
    sb.files.write("/home/user/.cf_token", tok)
    sb.files.write("/home/user/.e2b_key", os.environ["E2B_API_KEY"])
    sb.files.write("/home/user/.e2b_id", sid)
    sb.commands.run(
        "setsid bash /home/user/app/start.sh >/tmp/boot.log 2>&1 </dev/null &",
        timeout=60,
    )
    return sb


def spawn(template):
    log(f"creating sandbox from template {template}")
    sb = Sandbox.create(template=template, timeout=3600)
    log(f"created {sb.sandbox_id}")
    return sb


def wait_public(sb, wait_s):
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if public_ok():
            log(f"PUBLIC HEALTH OK after {int(time.time()-t0)}s")
            return True
        # nudge: if stack died, restart once
        if int(time.time() - t0) % 120 == 0:
            try:
                sb.commands.run("pgrep -f 'main.py --cpu' >/dev/null || setsid bash /home/user/app/start.sh >/tmp/boot.log 2>&1 </dev/null &", timeout=60)
            except Exception:
                pass
        time.sleep(10)
    return False


def main():
    if not os.environ.get("E2B_API_KEY") or not os.environ.get("CF_TUNNEL_TOKEN"):
        log("E2B_API_KEY / CF_TUNNEL_TOKEN missing")
        sys.exit(1)

    if public_ok():
        log("site healthy, checking sandbox expiry")
    else:
        log("site DOWN — will (re)deploy")

    boxes = running_boxes()
    log(f"running boxes: {[b['id'] for b in boxes]}")
    target = None
    for b in boxes:
        left = age_info(b)
        log(f"  {b['id']}: {left if left is None else round(left/60,1)} min left")
        if left is None or left > REPLACE_BEFORE_S:
            target = b
            break  # newest healthy box
    if target is None and boxes:
        # all boxes near expiry -> spawn replacement, keep old for overlap
        log("all boxes near expiry — spawning replacement (zero-downtime overlap)")
        sb = spawn(TEMPLATE)
        boot(sb)
        if wait_public(sb, HEALTH_WAIT_S):
            for b in boxes:
                try:
                    Sandbox.connect(b["id"]).kill()
                    log(f"killed old box {b['id']}")
                except Exception as e:
                    log(f"kill {b['id']} failed: {e}")
        return
    if target:
        log(f"using existing {target['id']} — verifying stack")
        # light-touch: ensure boot happened (token present + app process)
        try:
            sb = Sandbox.connect(target["id"])
            res = sb.commands.run("test -s /home/user/.cf_token && pgrep -f uvicorn >/dev/null && echo STACK_OK || echo STACK_DOWN", timeout=30)
            if "STACK_OK" not in res.stdout:
                log("stack down — rebooting")
                boot(sb)
                wait_public(sb, 300)
        except Exception as e:
            log(f"check failed on {target['id']}: {e}")
        return
    # no running box
    log("no running sandbox — fresh deploy")
    sb = spawn(TEMPLATE)
    boot(sb)
    ok = wait_public(sb, HEALTH_WAIT_S)
    log("DEPLOY " + ("SUCCESS" if ok else "FAILED (will retry next tick)"))
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
