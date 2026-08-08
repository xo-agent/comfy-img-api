import asyncio
import base64
import json
import os
import time
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

COMFY = "http://127.0.0.1:8188"
OUT = Path("/home/user/ComfyUI/output")
START = time.time()
JOBS = {}  # job_id -> dict
MAX_JOBS = 50

# Engine registry: model -> checkpoint + sensible defaults.
# Base res is deliberately small: the ESRGAN x2 upscale doubles it (768/1024 out),
# and keeping base low means the upscale pass fits in the 8GB box alongside the checkpoint.
MODELS = {
    "sd-turbo": {
        "ckpt": "sd_turbo.safetensors",
        "steps": 6,
        "cfg": 1.5,
        "width": 384,
        "height": 384,
        "max_steps": 10,
    },
    "sd15": {
        "ckpt": "v1-5-pruned-emaonly.safetensors",
        "steps": 20,
        "cfg": 7.0,
        "width": 512,
        "height": 512,
        "max_steps": 40,
    },
}
DEFAULT_MODEL = "sd-turbo"
UPSCALE_MODEL = "RealESRGAN_x2plus.pth"  # x2 only — x4plus at 512 base OOMs the 8GB box (512->2048)
UPSCALE_DIR = Path("/home/user/ComfyUI/models/upscale_models")

app = FastAPI(title="Ox-Img", version="2.0.0")


class GenRequest(BaseModel):
    prompt: str
    negative_prompt: str = "ugly, blurry, low quality, deformed, watermark, text, jpeg artifacts"
    model: str = DEFAULT_MODEL
    width: int = 0  # 0 -> model default
    height: int = 0
    steps: int = 0  # 0 -> model default
    cfg: float = 0.0  # 0 -> model default
    seed: int = -1
    batch: int = 1
    upscale: int = 2  # 1 = raw, 2 = RealESRGAN 2x upscale


def _post(path: str, payload: dict, timeout: int = 30):
    req = urllib.request.Request(
        COMFY + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _drain_queue(wait_s=60):
    """Wait until ComfyUI's queue is empty (no running/pending jobs)."""
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            q = _get("/queue", 5)
            if not q.get("queue_running") and not q.get("queue_pending"):
                return
        except Exception:
            pass
        time.sleep(2)


def _free_models():
    """Unload all checkpoints so the next model load starts from a clean slate."""
    try:
        req = urllib.request.Request(
            COMFY + "/free",
            data=b'{"unload_models": true, "free_memory": true}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except Exception:
        return None


def _comfy_alive():
    """True if ComfyUI answers system_stats (backend actually running)."""
    try:
        return "system" in _get("/system_stats", 5)
    except Exception:
        return False


MODEL_LOCK = asyncio.Lock()
LAST_MODEL = None


def _get(path: str, timeout: int = 15):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read())


def build_wf(p: GenRequest, seed: int) -> dict:
    m = MODELS.get(p.model)
    if not m:
        raise ValueError(f"unknown model {p.model!r}, use one of {list(MODELS)}")
    steps = int(p.steps) if p.steps and p.steps > 0 else m["steps"]
    cfg = float(p.cfg) if p.cfg and p.cfg > 0 else m["cfg"]
    w = int(p.width) if p.width and p.width > 0 else m["width"]
    h = int(p.height) if p.height and p.height > 0 else m["height"]
    steps = max(1, min(steps, m["max_steps"]))
    w = max(256, min(w, 768))
    h = max(256, min(h, 768))
    prefix = f"oximg_{int(time.time())}"  # unique per job — filenames must never collide (CF edge cache)
    wf = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": m["ckpt"]}},
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": w,
                "height": h,
                "batch_size": max(1, min(p.batch, 4)),
            },
        },
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": p.prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": p.negative_prompt, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": prefix, "images": ["8", 0]},
        },
    }
    # RealESRGAN x2 upscale pass (crisp detail; raw outputs are soft)
    upscale = max(1, min(int(p.upscale or 1), 2))
    if upscale == 2 and (UPSCALE_DIR / UPSCALE_MODEL).exists() and w * 2 <= 1536 and h * 2 <= 1536:
        wf["10"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": UPSCALE_MODEL}}
        wf["11"] = {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["10", 0], "image": ["8", 0]},
        }
        wf["9"]["inputs"]["images"] = ["11", 0]
    return wf


def _submit(req: GenRequest, seed: int):
    return _post("/prompt", {"prompt": build_wf(req, seed), "client_id": "ox-img-api"}, timeout=30)


def _poll_history(pid: str):
    try:
        return _get(f"/history/{pid}", timeout=10)
    except Exception:
        return {}


def _collect_images(h: dict, pid: str):
    images = []
    for node_id, node_out in h[pid]["outputs"].items():
        for img in node_out.get("images", []):
            fname = img["filename"]
            fpath = OUT / fname
            if fpath.exists():
                b64 = base64.b64encode(fpath.read_bytes()).decode()
                images.append({"name": fname, "url": f"/api/image/{fname}", "b64": b64})
    return images


async def _run_job(job_id: str, req: GenRequest):
    global LAST_MODEL
    job = JOBS[job_id]
    job["status"] = "running"
    seed = req.seed if req.seed and req.seed >= 0 else int(time.time() * 1000) % (2**32)
    job["seed"] = seed
    try:
        # model switch: free the previous checkpoint first (8GB box OOMs otherwise)
        async with MODEL_LOCK:
            if LAST_MODEL and LAST_MODEL != req.model:
                await asyncio.to_thread(_drain_queue)
                await asyncio.to_thread(_free_models)
            LAST_MODEL = req.model
        resp = await asyncio.to_thread(_submit, req, seed)
        pid = resp.get("prompt_id")
        if not pid:
            job["status"] = "error"
            job["error"] = f"no prompt_id: {resp}"
            return
        job["prompt_id"] = pid
        deadline = time.time() + 900
        fail_n = 0
        while time.time() < deadline:
            await asyncio.sleep(2)
            h = await asyncio.to_thread(_poll_history, pid)
            if h:
                fail_n = 0
                images = await asyncio.to_thread(_collect_images, h, pid)
                if images:
                    job.update(
                        {
                            "status": "done",
                            "elapsed_s": round(time.time() - job["created"], 1),
                            "images": images,
                        }
                    )
                    return
            else:
                fail_n += 1
                if fail_n >= 15:  # ~30s without history AND comfy unreachable -> backend died
                    alive = await asyncio.to_thread(_comfy_alive)
                    if not alive:
                        job["status"] = "error"
                        job["error"] = "backend restarting, please retry in a minute"
                        return
                    fail_n = 0
        job["status"] = "error"
        job["error"] = "timeout waiting for ComfyUI"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(Path("/home/user/app/index.html").read_text())


@app.get("/api/health")
async def health():
    comfy_ok = False
    try:
        st = await asyncio.to_thread(_get, "/system_stats", 5)
        comfy_ok = "system" in st
    except Exception:
        pass
    return {
        "status": "ok",
        "uptime_s": int(time.time() - START),
        "comfyui": comfy_ok,
        "models": list(MODELS),
        "jobs": {"active": sum(1 for j in JOBS.values() if j["status"] in ("queued", "running"))},
        "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


@app.get("/api/stats")
async def stats():
    try:
        q = await asyncio.to_thread(_get, "/queue", 5)
        running = len(q.get("queue_running", []))
        pending = len(q.get("queue_pending", []))
    except Exception:
        running = pending = -1
    return {"running": running, "pending": pending}


@app.post("/api/generate")
async def generate(req: GenRequest):
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(400, "prompt is required")
    if req.model not in MODELS:
        raise HTTPException(400, f"unknown model {req.model!r}, use one of {list(MODELS)}")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "queued",
        "created": time.time(),
        "params": req.model_dump(),
    }
    JOBS[job_id] = job
    if len(JOBS) > MAX_JOBS:
        for k in list(JOBS)[: len(JOBS) - MAX_JOBS]:
            JOBS.pop(k, None)
    asyncio.create_task(_run_job(job_id, req))
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/job/{job_id}")
async def job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    out = {k: v for k, v in j.items() if k != "images"}
    if j["status"] == "done":
        out["images"] = j.get("images", [])
        out["elapsed_s"] = j.get("elapsed_s")
    return out


@app.get("/api/image/{name}")
async def image(name: str):
    clean = Path(name).name
    fpath = OUT / clean
    if not fpath.exists():
        raise HTTPException(404, "image not found")
    # no-store: CF edge cached reused filenames -> users got stale 512 images
    return FileResponse(fpath, media_type="image/png", headers={"Cache-Control": "no-store, max-age=0"})
