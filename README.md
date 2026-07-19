# nl-image-gen

Natural-language image generation on top of the ComfyUI install this repo manages. Plain
text in, image out — no manual workflow/prompt tweaking. Deliberately self-contained (own
`.env`, own `Dockerfile`/`docker-compose.yml`, own dependencies) so it can be split into its
own repo later without touching the parent `comfyui-manager` project.

See `HISTORY.md` for the chronological record of what was built, why, and what was tried and
discarded — read that first if you're picking this up cold.

## Architecture

```
User (browser, phone or desktop)          AI agents (LAN, any machine)
        |                                          |
        v                                          v
  server.py (Flask, :5001) <---------- mcp_server/server.py (MCP, :8000/mcp)
        |                              thin wrapper, calls the same Flask API,
        |                              never talks to ComfyUI directly
        +--> llm_bridge.py  --> Ollama (192.168.50.46:11434, qwen3:8b)
        |     turns NL prompt into a structured generation "spec" JSON
        |
        +--> workflow_builder.py
        |     turns the spec into a ComfyUI node graph (deterministic, no LLM)
        |
        +--> comfy_client.py --> ComfyUI (192.168.50.121:8188)
              submits the graph, streams live progress via websocket,
              fetches the finished image via ComfyUI's own /view endpoint
```

Both entry points go through `server.py`'s single in-process job queue, which is what
serializes generation against the shared GPU-backed ComfyUI instance. The MCP server must
never call ComfyUI or `comfy_client.py` directly — see `HISTORY.md` Session 4 for why.

Key design choice: the LLM never builds the ComfyUI graph directly (too brittle — wrong
node IDs, malformed connections). It only fills in a flat spec (prompt text, checkpoint,
size, sampler params, a few feature flags). Code deterministically turns that into the
actual graph. This is what makes the pipeline reliable enough for "just works."

## Files

- `config.py` — env-var loader (this app's own port, Comfy/Ollama hosts, gallery DB path). No
  local filesystem paths for ComfyUI's output — everything is fetched over HTTP so this app
  is fully decoupled from wherever ComfyUI itself runs.
- `comfy_client.py` — ComfyUI REST + websocket client. `submit_workflow`, `wait_for_result`
  (HTTP-polling fallback), `wait_for_result_ws` (live per-step progress, primary path),
  `list_checkpoints`.
- `llm_bridge.py` — `build_spec()` (NL prompt -> generation spec via Ollama) and
  `assess_ambiguity()` (pre-flight check that asks a clarifying question instead of
  guessing, when a request is genuinely ambiguous). Has a verbatim-inclusion safety net and
  negation-routing logic — see `HISTORY.md` for why these exist, they're not obvious from
  the code alone.
- `workflow_builder.py` — spec -> ComfyUI graph. Composable optional stages: `hires` (latent
  upscale + refine pass) and `face_fix` (Impact Pack FaceDetailer pass). Both LLM-controlled
  via spec flags, both no-ops when not needed.
- `server.py` — Flask app. In-process job queue (single worker thread — the shared GPU can
  only run one generation at a time regardless of how many people submit). SQLite gallery
  history. `/image/<name>` proxies ComfyUI's `/view` endpoint rather than reading local
  disk, so this app has zero filesystem dependency on the ComfyUI host. `/checkpoints`
  returns the installed ComfyUI checkpoint filenames (informational only — nothing accepts a
  checkpoint override today, `llm_bridge.py` always auto-picks one).
- `templates/index.html` — Vue 3 via CDN, no build step. Textarea, clarify-question flow,
  live progress bar, gallery grid.
- `generate.py` — CLI entrypoint, same building blocks as `server.py`. Useful for quick
  testing without spinning up the web app.
- `Dockerfile` / `docker-compose.yml` — deploy target is a Docker host (e.g. Unraid), not
  the Mac itself. See `HISTORY.md` for why this changed from an original macOS-LaunchAgent
  plan.
- `mcp_server/` — MCP server (official `mcp` SDK, FastMCP, Streamable HTTP transport) giving
  AI agents on the LAN tool access to this app: `generate_image`, `check_image_status`,
  `list_recent_generations`, `list_checkpoints`. A thin HTTP wrapper only — calls this app's
  own Flask API, same as the browser UI does, so the single-worker job queue above still
  serializes everything.
  Own `requirements.txt`/`Dockerfile`, built as a second service (`nl-image-gen-mcp`) in
  `docker-compose.yml`. Uses two separate base-URL env vars (`NL_IMAGE_GEN_URL` for its own
  internal calls via Compose DNS, `NL_IMAGE_GEN_PUBLIC_URL` for building image URLs that
  other LAN machines can actually load) — see `HISTORY.md` Session 4 for why one URL isn't
  enough.

## Running

**CLI (fastest for testing changes):**
```bash
eval "$(conda shell.zsh hook)" && conda activate comfy
python3 generate.py "a golden retriever at sunset"
```

**Web app, locally via Docker:**
```bash
docker compose up -d --build
# http://<this-host-ip>:5001
```
This also brings up the MCP server (`nl-image-gen-mcp`) at `http://<this-host-ip>:8000/mcp`
for AI agents on the LAN — connect any MCP client (Streamable HTTP transport) to that URL.

Both ports are configurable, not hardcoded: set `PORT` (Flask app, default `5001`) and/or
`MCP_PORT` (MCP server, default `8000`) in `.env` — Compose reads that same file for both the
container env var and the host port mapping, so changing one line moves the service
everywhere consistently (useful if `5001` or `8000` is already taken on the deploy host, e.g.
Unraid).

## Current ComfyUI-side state (as of last session)

- Checkpoints: `dreamshaperXL_lightningDPMSDE.safetensors` (fast default, 6-10 steps),
  `juggernautXL_ragnarok.safetensors` (general/quality default, 20-30 steps + karras).
- Custom nodes installed: ComfyUI-Impact-Pack + ComfyUI-Impact-Subpack (face detection/
  refinement), with `face_yolov8m.pt` in `models/ultralytics/bbox/`.
- LoRAs: `add-detail-xl.safetensors` (detail), `CinematicStyle_v1.safetensors` (cinematic),
  `anime-enhancer-xl.safetensors` (anime). The LLM picks 0-2 per prompt automatically (see
  `HISTORY.md` Session 6).
- ComfyUI's own LaunchAgent (`~/Library/LaunchAgents/com.local.run-comfyui.plist`) had a
  broken `ProgramArguments` path (fixed last session) — if ComfyUI is ever unreachable and
  `make status` shows it not running, check `launchctl print gui/$(id -u)/com.local.run-comfyui`
  for `last exit code` before assuming it's a code problem.
