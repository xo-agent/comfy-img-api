import os, time, json, urllib.request
os.environ.setdefault("E2B_API_KEY", os.environ.get("E2B_API_KEY", ""))
from e2b import Sandbox

TOKEN = os.environ["CF_TUNNEL_TOKEN"]
SITE = "https://ox-img.oxu.indevs.in"

def log(m): print(f"[deploy {time.strftime('%H:%M:%S')}] {m}", flush=True)

def public_ok():
    try:
        with urllib.request.urlopen(SITE + "/api/health", timeout=20) as r:
            return r.status == 200, r.read()[:200]
    except Exception as e:
        return False, str(e)[:120]

sb = Sandbox.create(template="comfy-serve", timeout=3600)
sid = sb.sandbox_id
log(f"spawned {sid}")
sb.files.write("/home/user/.cf_token", TOKEN)
sb.files.write("/home/user/.e2b_key", os.environ["E2B_API_KEY"])
sb.files.write("/home/user/.e2b_id", sid)
log("secrets injected")
sb.commands.run("setsid bash /home/user/app/start.sh >/tmp/boot.log 2>&1 </dev/null &", timeout=60)
log("boot started")

t0 = time.time()
while time.time() - t0 < 420:
    time.sleep(15)
    ok, body = public_ok()
    if ok:
        log(f"PUBLIC HEALTH OK after {int(time.time()-t0)}s")
        break
    # peek boot log every minute
    if int(time.time() - t0) % 60 == 0:
        r = sb.commands.run("tail -3 /tmp/boot.log 2>/dev/null; echo ---CF---; tail -3 /tmp/cf.log 2>/dev/null; echo ---APP---; tail -3 /tmp/app.log 2>/dev/null", timeout=30)
        print(r.stdout, flush=True)
else:
    log("HEALTH TIMEOUT")
    r = sb.commands.run("tail -20 /tmp/boot.log; echo ---CF---; tail -10 /tmp/cf.log; echo ---APP---; tail -10 /tmp/app.log", timeout=30)
    print(r.stdout, flush=True)

print("SID=" + sid, flush=True)
