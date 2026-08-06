# Ox-Img — 100%-uptime image generation on E2B + GitHub Actions + Cloudflare Tunnel

Free image-generation **website** + **API** served from an E2B sandbox (free tier,
CPU-only SD1.5) exposed through a Cloudflare Tunnel on your own domain.

## Architecture

```
Browser / curl ──> https://ox-img.oxu.indevs.in
                      │  Cloudflare Tunnel (cloudflared in sandbox, token from GH secret)
                      ▼
              FastAPI :80  (site UI + /api/generate + /api/health — tunnel route target)
                      │  localhost
                      ▼
              ComfyUI :8188 (SD1.5 txt2img, CPU)

GitHub Actions watchdog (cron */45 + on:push)
   └─> E2B API: list running sandboxes → spawn/boot/swap before the 1h free-tier cap
```

## Uptime design (free tier reality)

| Constraint | Countermeasure |
|---|---|
| 1h hard cap per sandbox (`timeout` > 3600 → 400) | Watchdog spawns a replacement ~20 min before expiry |
| Same CF tunnel token supports multiple connectors | Replacement boots while old still serves → **zero-downtime swap** |
| Idle boxes get auto-paused/reaped | `keepalive.py` self-pings the E2B API every ~100 s + watchdog verifies stack |
| Sandbox FS is ephemeral | Custom E2B template `comfy-serve` (ComfyUI + model + app baked in) → ~30 s resurrection |
| Free GH Actions budget (2000 min/mo public) | 45-min cron, healthy-path runs ~1 min |

## Endpoints

- `GET /` — the UI (prompt, presets, steps/CFG/size/batch/seed sliders)
- `POST /api/generate` — `{"prompt": "...", "negative_prompt": "...", "width": 384, "height": 384, "steps": 16, "cfg": 7.0, "seed": -1, "batch": 1}` → `{"job_id": "...", "status": "queued"}` (async — CF's 100 s origin timeout kills sync gen on CPU)
- `GET /api/job/<job_id>` — poll: `{"status": "queued|running|done|error", "seed", "elapsed_s", "images": [{"name","url","b64"}]}`
- `GET /api/image/<file>` — generated PNG
- `GET /api/health` — status + ComfyUI liveness
- `GET /api/stats` — ComfyUI queue depth

## Repo layout

- `app/` — FastAPI app (`main.py`), UI (`index.html`), boot script (`start.sh`), anti-idle `keepalive.py`
- `scripts/provision.sh` — one-shot stack installer (run inside a sandbox)
- `scripts/build_template.py` — provision + snapshot → E2B template `comfy-serve`
- `scripts/watchdog.py` — GH Actions uptime brain
- `.github/workflows/` — `watchdog.yml` (45-min cron + push), `build-template.yml` (manual rebuild)

## Secrets (GitHub Actions → repo)

| Secret | Value |
|---|---|
| `E2B_API_KEY` | E2B free-tier key |
| `CF_TUNNEL_TOKEN` | Cloudflare tunnel token for `ox-img.oxu.indevs.in` |

## Operations

- Rebuild template (after dep changes): Actions → `build-template` → Run workflow
- Redeploy app code: `git push` (watchdog pulls latest code into the live sandbox)
- Force check: Actions → `watchdog` → Run workflow
