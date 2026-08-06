#!/usr/bin/env python3
"""Build the E2B template with the full stack baked in:
ComfyUI (CPU) + SD1.5 checkpoint + FastAPI app + cloudflared + keepalive.

Runs from GitHub Actions (build-template.yml) or locally:
    E2B_API_KEY=... python3 scripts/build_template.py

Steps: provision sandbox -> run scripts/provision.sh -> create_snapshot -> verify spawn.
"""
import os
import sys
import time

os.environ.setdefault("E2B_API_KEY", os.environ.get("E2B_API_KEY", ""))
from e2b import Sandbox  # noqa: E402

NAME = os.environ.get("TEMPLATE_NAME", "comfy-serve")
PROVISION = os.environ.get(
    "PROVISION_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "provision.sh"),
)


def log(m):
    print(f"[build {time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    if not os.environ.get("E2B_API_KEY"):
        log("E2B_API_KEY missing")
        sys.exit(1)

    sb = Sandbox.create(template="desktop", timeout=3600)
    sid = sb.sandbox_id
    log(f"provision sandbox: {sid}")

    with open(PROVISION) as f:
        sb.files.write("/tmp/provision.sh", f.read())
    sb.commands.run(
        "setsid bash /tmp/provision.sh >/tmp/provision.log 2>&1 </dev/null &",
        timeout=60,
    )

    # poll for PROVISION DONE (up to 30 min)
    t0 = time.time()
    while time.time() - t0 < 1800:
        time.sleep(30)
        try:
            r = sb.commands.run(
                "grep -c 'PROVISION DONE' /tmp/provision.log 2>/dev/null || echo 0; tail -1 /tmp/provision.log",
                timeout=60,
            )
            lines = r.stdout.strip().splitlines()
            if lines and lines[0].strip() == "1":
                log("provision DONE")
                break
            if int(time.time() - t0) % 300 == 0:
                log(f"still provisioning... last: {lines[-1] if lines else ''}")
        except Exception as e:
            log(f"poll error: {e}")
    else:
        log("PROVISION TIMEOUT — dumping log tail")
        print(sb.commands.run("tail -40 /tmp/provision.log", timeout=60).stdout)
        sb.kill()
        sys.exit(2)

    log("creating snapshot...")
    snap = sb.create_snapshot(name=NAME)
    log(f"snapshot: {snap}")

    # verify spawn from the new template
    log(f"verifying spawn from template '{NAME}'...")
    try:
        t = Sandbox.create(template=NAME, timeout=3600)
        log(f"spawn OK: {t.sandbox_id}")
        t.kill()
    except Exception as e:
        log(f"SPAWN VERIFY FAILED: {type(e).__name__}: {e}")
        # try by snapshot id
        try:
            snap_id = getattr(snap, "snapshot_id", None) or getattr(snap, "template_id", None) or str(snap)
            t2 = Sandbox.create(template=snap_id, timeout=3600)
            log(f"spawn by id OK: {t2.sandbox_id}")
            t2.kill()
        except Exception as e2:
            log(f"spawn by id failed too: {e2}")

    sb.kill()
    log("BUILD COMPLETE")


if __name__ == "__main__":
    main()
