#!/usr/bin/env python3
"""Keepalive + self-replacement daemon (runs INSIDE the sandbox).

1. Every ~100s: self-connect ping -> E2B sees activity -> never auto-paused.
2. When < SELF_REPLACE_S left in the 60-min lifetime: spawn a fresh box from the
   template, inject secrets, boot it, verify it serves, then kill this box.

Uptime no longer depends on external schedulers (GitHub cron is just the safety net).
"""
import os
import sys
import time

E2B_API_KEY = ""
OWN_ID = ""
TEMPLATE = "comfy-serve"
BOOT_S = 3600
SELF_REPLACE_S = 20 * 60  # replace when < 20 min of the 60-min lifetime remains
START = time.time()


def log(msg):
    print(f"[keepalive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect():
    from e2b import Sandbox

    return Sandbox.connect(OWN_ID)


def ping(sb):
    r = sb.commands.run("true", timeout=30)
    return r.exit_code == 0


def remaining():
    return BOOT_S - (time.time() - START)


def spawn_replacement():
    """Create a new box, inject secrets, boot it. Returns new box or None."""
    from e2b import Sandbox

    cf_token = open("/home/user/.cf_token").read().strip()
    e2b_key = open("/home/user/.e2b_key").read().strip()
    for attempt in range(3):
        try:
            log(f"spawning replacement (attempt {attempt+1})")
            new = Sandbox.create(template=TEMPLATE, timeout=BOOT_S)
            new.files.write("/home/user/.cf_token", cf_token)
            new.files.write("/home/user/.e2b_key", e2b_key)
            new.files.write("/home/user/.e2b_id", new.sandbox_id)
            new.commands.run(
                "setsid bash /home/user/app/start.sh >/tmp/boot.log 2>&1 </dev/null &",
                timeout=60,
            )
            log(f"replacement {new.sandbox_id} booting")
            return new
        except Exception as e:
            log(f"spawn failed: {str(e)[:120]}")
            time.sleep(30)
    return None


def replacement_ready(new, wait_s=180):
    """True once the new box's app answers on :80 and cloudflared registered."""
    from e2b import Sandbox

    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            n = Sandbox.connect(new.sandbox_id)
            r = n.commands.run(
                "curl -s -m 5 http://127.0.0.1:80/api/health | grep -q comfyui && "
                "tail -3 /tmp/cf.log | grep -q 'Registered tunnel connection' && echo READY || echo NOT_READY",
                timeout=30,
            )
            if "READY" in r.stdout:
                return True
        except Exception:
            pass
        time.sleep(20)
    return False


def kill_self():
    from e2b import Sandbox

    try:
        Sandbox.kill(OWN_ID)
        log("self-killed")
    except Exception as e:
        log(f"self-kill failed ({str(e)[:80]}), exiting anyway")
        os._exit(0)


def main():
    global E2B_API_KEY, OWN_ID
    try:
        E2B_API_KEY = open("/home/user/.e2b_key").read().strip()
        OWN_ID = open("/home/user/.e2b_id").read().strip()
    except Exception as e:
        print(f"secrets missing: {e}", flush=True)
        sys.exit(1)
    os.environ["E2B_API_KEY"] = E2B_API_KEY
    log(f"keepalive started for {OWN_ID}, lifetime {BOOT_S}s, replace at <{SELF_REPLACE_S}s")

    replaced = False
    ping_n = 0
    while True:
        try:
            ping_n += 1
            sb = connect()
            ok = ping(sb)
            if ping_n % 6 == 0 or not ok:  # log every ~10 min and on failures
                log(f"ping {'ok' if ok else 'FAILED'} exit={getattr(sb, 'last_exit', None)}")
            sb.close() if hasattr(sb, "close") else None
        except Exception as e:
            log(f"ping failed: {str(e)[:100]}")

        if not replaced and remaining() < SELF_REPLACE_S:
            log(f"replacing: {int(remaining())}s left")
            new = spawn_replacement()
            if new is not None:
                ready = replacement_ready(new)
                if ready:
                    log("replacement verified READY")
                    kill_self()
                    return
                else:
                    log("replacement NOT ready in time; killing it and retrying later")
                    try:
                        from e2b import Sandbox

                        Sandbox.kill(new.sandbox_id)
                    except Exception:
                        pass
            # retry the whole replacement cycle next loop
            time.sleep(30)
            continue

        time.sleep(100)


if __name__ == "__main__":
    main()
