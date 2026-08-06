import os, time, urllib.request
os.environ.setdefault("E2B_API_KEY", os.environ.get("E2B_API_KEY", ""))
from e2b import Sandbox

SID = os.environ["DEPLOY_SID"]
SITE = "https://ox-img.oxu.indevs.in"

def log(m): print(f"[mon {time.strftime('%H:%M:%S')}] {m}", flush=True)

def public_ok():
    try:
        with urllib.request.urlopen(SITE + "/api/health", timeout=20) as r:
            return r.status == 200, r.read().decode()[:200]
    except Exception as e:
        return False, str(e)[:120]

sb = Sandbox.connect(SID)
t0 = time.time()
while time.time() - t0 < 420:
    time.sleep(20)
    ok, body = public_ok()
    if ok:
        log(f"PUBLIC HEALTH OK after {int(time.time()-t0)}s: {body}")
        break
    r = sb.commands.run("tail -2 /tmp/boot.log 2>/dev/null || echo no-bootlog; echo -n 'CF: '; tail -1 /tmp/cf.log 2>/dev/null || echo no-cflog; echo -n 'APP: '; tail -1 /tmp/app.log 2>/dev/null || echo no-applog; echo -n 'COMFY: '; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8188/system_stats 2>/dev/null || echo down", timeout=30)
    print("--- " + r.stdout.strip(), flush=True)
else:
    log("TIMEOUT — final logs:")
    print(sb.commands.run("tail -15 /tmp/boot.log 2>/dev/null || echo no-bootlog; echo ---CF---; tail -15 /tmp/cf.log 2>/dev/null || echo no-cflog; echo ---APP---; tail -15 /tmp/app.log 2>/dev/null || echo no-applog", timeout=30).stdout, flush=True)
