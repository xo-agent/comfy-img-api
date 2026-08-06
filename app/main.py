import asyncio
import base64
import json
import os
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

COMFY = "http://127.0.0.1:8188"
OUT = Path("/home/user/ComfyUI/output")
CKPT = "v1-5-pruned-emaonly.safetensors"
START = time.time()

app = FastAPI(title="Ox-Img", version="1.0.0")


class GenRequest(BaseModel):
    prompt: str
    negative_prompt: str = "ugly, blurry, low quality, deformed, watermark, text, jpeg artifacts"
    width: int = 384
    height: int = 384
    steps: int = 16
    cfg: float = 7.0
    seed: int = -1
    batch: int = 1


def _post(path: str, payload: dict, timeout: int = 30):
    req = urllib.request.Request(
        COMFY + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(path: str, timeout: int = 15):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read())


def build_wf(p: GenRequest, seed: int) -> dict:
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": max(1, min(p.steps, 40)),
                "cfg": max(1.0, min(p.cfg, 15.0)),
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": max(256, min(p.width, 768)),
                "height": max(256, min(p.height, 768)),
                "batch_size": max(1, min(p.batch, 4)),
            },
        },
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": p.prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": p.negative_prompt, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "oximg", "images": ["8", 0]},
        },
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(Path("/home/user/app/index.html").read_text())


@app.get("/api/health")
async def health():
    comfy_ok = False
    try:
        st = _get("/system_stats", timeout=5)
        comfy_ok = "system" in st
    except Exception:
        pass
    return {
        "status": "ok",
        "uptime_s": int(time.time() - START),
        "comfyui": comfy_ok,
        "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


@app.get("/api/stats")
async def stats():
    try:
        q = _get("/queue", timeout=5)
        running = len(q.get("queue_running", []))
        pending = len(q.get("queue_pending", []))
    except Exception:
        running = pending = -1
    return {"running": running, "pending": pending}


@app.post("/api/generate")
async def generate(req: GenRequest):
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(400, "prompt is required")
    seed = req.seed if req.seed and req.seed >= 0 else int(time.time() * 1000) % (2**32)
    t0 = time.time()
    try:
        resp = _post(
            "/prompt",
            {"prompt": build_wf(req, seed), "client_id": "ox-img-api"},
            timeout=30,
        )
    except Exception as e:
        raise HTTPException(503, f"comfyui submit failed: {e}")
    pid = resp.get("prompt_id")
    if not pid:
        raise HTTPException(500, f"no prompt_id: {resp}")

    deadline = time.time() + 600
    images = []
    while time.time() < deadline:
        try:
            h = _get(f"/history/{pid}", timeout=10)
        except Exception:
            h = {}
        if h:
            for node_id, node_out in h[pid]["outputs"].items():
                for img in node_out.get("images", []):
                    fname = img["filename"]
                    fpath = OUT / fname
                    if fpath.exists():
                        b64 = base64.b64encode(fpath.read_bytes()).decode()
                        images.append(
                            {"name": fname, "url": f"/api/image/{fname}", "b64": b64}
                        )
            break
        await asyncio.sleep(2)

    if not images:
        raise HTTPException(504, "generation timed out on CPU (queue too long?)")
    return {
        "status": "success",
        "prompt_id": pid,
        "seed": seed,
        "elapsed_s": round(time.time() - t0, 1),
        "width": req.width,
        "height": req.height,
        "steps": req.steps,
        "images": images,
    }


@app.get("/api/image/{name}")
async def image(name: str):
    # safe path join — refuse traversal
    clean = Path(name).name
    fpath = OUT / clean
    if not fpath.exists():
        raise HTTPException(404, "image not found")
    return FileResponse(fpath, media_type="image/png")
