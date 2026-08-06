#!/usr/bin/env python3
"""Anti-idle keepalive: free-tier E2B pauses sandboxes after ~5 min without API
traffic. This pings the E2B API from inside the sandbox (self-connect + trivial
command) every ~100s so the box stays unpaused while serving the public site."""
import os
import time
from pathlib import Path

KEY = Path("/home/user/.e2b_key").read_text().strip() if Path("/home/user/.e2b_key").exists() else os.environ.get("E2B_API_KEY", "")
SID = Path("/home/user/.e2b_id").read_text().strip() if Path("/home/user/.e2b_id").exists() else ""

if not KEY or not SID:
    while True:  # wait for watchdog to inject
        KEY = Path("/home/user/.e2b_key").read_text().strip() if Path("/home/user/.e2b_key").exists() else ""
        SID = Path("/home/user/.e2b_id").read_text().strip() if Path("/home/user/.e2b_id").exists() else ""
        if KEY and SID:
            break
        time.sleep(10)

os.environ["E2B_API_KEY"] = KEY
from e2b import Sandbox  # noqa: E402

tick = 0
while True:
    try:
        sb = Sandbox.connect(SID, timeout=30)
        r = sb.commands.run("true", timeout=30)
        if tick % 6 == 0:
            print(f"[keepalive {time.strftime('%H:%M:%S')}] ping ok exit={r.exit_code}", flush=True)
    except Exception as e:
        print(f"[keepalive {time.strftime('%H:%M:%S')}] ping failed: {type(e).__name__}: {str(e)[:100]}", flush=True)
    tick += 1
    time.sleep(100)
