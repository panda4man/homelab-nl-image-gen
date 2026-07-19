# History

Chronological record of how `nl-image-gen` came to be, in order. Read this if you're a
fresh agent session picking this up — a lot of the current code shape is a direct response
to something that failed in testing, and isn't obvious from reading the code alone.

## Session 1: initial pipeline

Goal stated by the user: bridge their local LLM/embedding server (Ollama, on a separate
host) to the ComfyUI install this repo manages, so natural-language text could become an
image with minimal manual prompt/workflow engineering.

Considered three approaches: (1) direct API bridge with hardcoded templates, (2) LLM builds
the full ComfyUI workflow graph itself, (3) hybrid template-routing. Went with a variant of
(2), but constrained: the LLM never emits the actual ComfyUI graph (node IDs, connection
slots) — that's brittle even for capable models. Instead the LLM fills in a flat spec
(prompt text + generation params), and code deterministically builds the graph. This
decision held up for the rest of the project and is the core reason the pipeline is
reliable.

Built the initial four files: `config.py`, `comfy_client.py`, `llm_bridge.py`,
`workflow_builder.py`, `generate.py`. Verified end-to-end against the live ComfyUI instance
(192.168.50.121:8188) and Ollama (192.168.50.46:11434, models `qwen3:8b` / `qwen2.5:7b`).

**Bug found immediately:** the LLM would paraphrase away named subjects/objects from the
user's prompt (e.g. a prompt naming "Thor's mighty lightning hammer" came back without any
hammer at all). Fixed with a verbatim-inclusion safety net in `llm_bridge.py`: if the user's
literal text isn't a substring of the LLM's `positive_prompt` output, force-prepend it.

**Stress test: chibi mushroom-creature wielding a hammer.** Used as a running test case
across several iterations because it exposed real limits:
- Asking for "no legs, no human face" *inside the positive prompt* (even via the verbatim
  safety net) backfired — Stable Diffusion attends to the named noun and often reinforces
  the unwanted concept instead of excluding it, rather than actually excluding it.
- Manually routing those exclusions into `negative_prompt` (true negative conditioning)
  fixed the anatomy issue, but revealed a second failure mode: over-broad negative terms
  ("axe, spear" to prevent weapon-substitution) suppressed the *wanted* object (the hammer)
  too, because they sit close together in the model's embedding space.
- Conclusion at the time: no amount of one-shot prompt engineering reliably nails highly
  novel *compound* creative asks (unusual anatomy nobody trained the model on) — this is a
  real ceiling, not a bug. Addressed properly in a later session via a clarifying-question
  pre-flight (see below) rather than more prompt tweaking.

Also evaluated `ChimeraMi(XL)`, a creature/hybrid-focused checkpoint, hoping it would help
with the mushroom test. It didn't meaningfully outperform the general checkpoint even in its
own niche — flagged for later replacement.

## Session 2: from CLI to a family-facing web app

User's actual goal, stated directly: a general-purpose image generator for themselves and
family — plain text in, image out, reachable from a browser, "just works" for everyday
requests without per-query tweaking.

**Design pivot during planning (important — don't rebuild this the old way):** originally
assumed this would run on the Mac via macOS LaunchAgent, mirroring how ComfyUI itself runs.
Mid-planning, learned the user actually runs a Docker/Unraid home server for their other
self-hosted services and wanted this deployed there instead — not tied to the Mac. This
works because ComfyUI exposes a `GET /view?filename=...&type=output` endpoint that serves
generated images by filename over plain HTTP, so the web app never needs filesystem access
to wherever ComfyUI runs. **Do not reintroduce a local-filesystem dependency** (e.g. don't
resurrect the old `COMFY_OUTPUT_DIR` config or scan a local output directory for the
gallery) — that was deliberately removed specifically to keep this app host-agnostic.

Also considered Nuxt for the frontend. Rejected in favor of Vue 3 loaded via CDN directly in
`templates/index.html` (no build step, no Node process to manage) — the "second service to
babysit" argument against Nuxt mostly evaporates once everything is a Docker container
anyway (Unraid treats any container uniformly), but a zero-build single-file frontend was
still the better fit for a one-page app.

Built: negation-routing fix in `llm_bridge.py`'s `SYSTEM_PROMPT` (exclusions go to
`negative_prompt` in positive form, e.g. "no human face" -> negative prompt gets "human
face"), a `_strip_negations()` regex helper so the verbatim safety net stops reinjecting
negation phrasing into `positive_prompt`, and `assess_ambiguity()` — a small pre-flight
Ollama call that asks the user ONE clarifying question when a request is genuinely ambiguous
(capped at one round, so it never becomes a wizard), rather than letting the main generation
call silently guess. This is the real fix for the mushroom-test class of problem: ask
instead of guess.

Built `server.py` (Flask, job queue + single worker thread since the shared GPU can only run
one generation at a time), `templates/index.html` (Vue-CDN UI), `Dockerfile`/
`docker-compose.yml`. SQLite gallery history (not a filesystem scan — see above).

Verified end-to-end via `docker compose build && up`, including job serialization (two
concurrent submissions correctly queue rather than race), gallery persistence across
container restart (volume-mounted DB), and the `/image/<name>` route rejecting path-traversal/
injection attempts.

**Found and fixed along the way:** Jinja2 and Vue both use `{{ }}` syntax. Routing
`index.html` through Flask's `render_template` caused a 500 on every load because Jinja
tried to server-side-resolve Vue template expressions. Fixed by serving the file as a raw
static response — no server-side templating was ever actually needed for a pure
client-side-rendered page.

## Session 3: real-time progress, checkpoint swap, image-quality passes

**Real-time progress bar.** Discovered ComfyUI exposes a websocket API
(`ws://host:port/ws?clientId=<id>`) streaming live per-step `progress` events (used by
ComfyUI's own UI). Replaced the old HTTP-polling-only wait with
`comfy_client.wait_for_result_ws()` (primary path, with the original `wait_for_result()`
HTTP-polling function kept as a fallback if the websocket connection itself fails). `/status`
now returns `step`/`total_steps`, and the frontend shows an actual progress bar instead of
just a spinner — meaningfully more useful once hi-res/face-fix passes made some requests
take 1-3 minutes.

**Checkpoint swap.** Removed `chimeramiXL_v66.safetensors` (see Session 1 — never
outperformed the general checkpoint even in its own creature/hybrid niche) and added
`juggernautXL_ragnarok.safetensors` (1.5M+ downloads on CivitAI, the standard general-purpose
photoreal SDXL checkpoint) as the "quality" option alongside the existing
`dreamshaperXL_lightningDPMSDE.safetensors` "fast" option.

**Karras scheduler + real SDXL resolution buckets.** Two cheap, high-value fixes:
- The LLM used to pick arbitrary `width`/`height` (including 512x512, an SD1.5-era
  resolution SDXL wasn't trained on). Replaced with an `orientation` field
  (square/portrait/landscape, LLM picks based on subject) mapped in code to real SDXL
  training buckets (`_ORIENTATION_BUCKETS` in `llm_bridge.py`: 1024x1024 / 896x1152 /
  1152x896).
- Scheduler was hardcoded to `"normal"` everywhere. Lightning/turbo-distilled checkpoints
  and standard SDXL checkpoints want different schedulers (`sgm_uniform` vs `karras`
  respectively) — `_scheduler_for_checkpoint()` in `llm_bridge.py` picks this
  deterministically from the checkpoint name, not via the LLM (more reliable for a rule
  that's actually deterministic). Verified visibly better output on both a landscape and a
  portrait test render.

**Face detailer pass.** Installed `ComfyUI-Impact-Pack` + `ComfyUI-Impact-Subpack` (git
clone into `custom_nodes/`, `pip install -r requirements.txt` in the `comfy` conda env —
**no ComfyUI-Manager UI was present on this instance** despite the parent repo's README
claiming it's installed by `install-comfyui.sh`; went with the standard manual
git-clone-and-pip-install method instead, which works regardless). Downloaded
`face_yolov8m.pt` from `huggingface.co/Bingsu/adetailer` (the source the Impact-Subpack
README itself recommends) into `models/ultralytics/bbox/`. Wired a `face_fix` spec flag
(LLM sets it true when a face is likely in-frame) that appends a `UltralyticsDetectorProvider`
+ `FaceDetailer` stage in `workflow_builder.py`, composable with the existing `hires` stage
(base -> [hires] -> [face_fix] -> save). Verified on two real portraits — visibly sharper
eyes/skin/facial structure both times.

**Found and fixed along the way (unrelated to the above, pre-existing bug):** restarting
ComfyUI to load the new custom nodes revealed its own LaunchAgent
(`~/Library/LaunchAgents/com.local.run-comfyui.plist`) pointed at a broken path
(`/Users/aclinton/Dev/AI/Comfy/run-comfyui` — missing "UI", never existed). ComfyUI had
apparently been running from some earlier manual start that predated this session and never
needed the LaunchAgent to actually respawn it until it was explicitly restarted. Fixed the
plist to point at the correct path (`/Users/aclinton/Dev/AI/comfyui-manager/run-comfyui`)
and confirmed `launchctl print` shows `state = running` with proper supervision restored.

## Session 4: MCP server for agent access

Picked up the deferred open item from Session 3: let other AI agents (not just the human web
UI), running on any machine on the LAN — work machines, or agents running on the Unraid box
itself — generate images as a tool call. Confirmed the earlier design direction still held
and built it: a thin MCP server (`mcp_server/`) that wraps `server.py`'s existing Flask API
(`/generate`, `/status/<job_id>`, `/gallery`) rather than talking to ComfyUI directly. Two
independent processes both submitting jobs straight to ComfyUI would have broken the
single-worker-thread GPU serialization `server.py` guarantees — the MCP server never touches
`comfy_client.py` or ComfyUI, only the Flask HTTP API, same as the browser UI.

**Transport: centrally-hosted Streamable HTTP, not stdio.** Considered a stdio MCP server
installed per-machine (each agent's MCP client spawns it locally, it calls out to the Flask
API over HTTP). Rejected in favor of one MCP server, deployed as a second container
(`nl-image-gen-mcp`) alongside the existing `nl-image-gen` service on Unraid, reachable by
every agent on the LAN at a single fixed URL. No per-machine install, and it mirrors how the
Flask app itself is already deployed (one Docker service, host-agnostic). Used the official
`mcp` Python SDK's `FastMCP` with `transport="streamable-http"` — pinned
`mcp>=1.12,<2` since a 2.0 pre-release exists with a breaking API rework and isn't stable yet.

**Three tools, async by design.** `generate_image` (prompt in, job_id out — returns
immediately, doesn't block on a 15s-3min render) and `check_image_status` (job_id in,
progress/result out), matching the two-tool shape already agreed in Session 3. Added a third,
`list_recent_generations`, wrapping `/gallery`: job state in `server.py` lives only in an
in-memory dict, so once a job_id is forgotten (service restart, or an agent in a later
conversation turn that never saw the original job_id), the SQLite gallery is the *only*
persistent way to retrieve a past image's URL. Without this tool an agent would have no way
to answer "what did you generate for me yesterday."

**Clarify flow exposed verbatim.** `/generate` sometimes returns a clarifying question
instead of queuing a job (see Session 2's `assess_ambiguity()`). Rather than defaulting the
MCP tool to always skip that check, `generate_image` surfaces `{"status": "clarify",
"question": ...}` exactly as the API does — the calling agent (itself an LLM) can read the
question and re-call with more detail and `clarified=true`. Skipping it would have quietly
reintroduced the ambiguous-prompt failure mode Session 2 specifically fixed.

**The two-base-URL gotcha.** The MCP server needs to both call the Flask app internally
*and* hand back an `image_url` the calling agent can load itself — and those are not the
same address. Internal calls use `NL_IMAGE_GEN_URL` (the Docker Compose service DNS name,
`http://nl-image-gen:5001`, only resolvable inside the compose network). The `image_url`
returned to agents is built from `NL_IMAGE_GEN_PUBLIC_URL` (the Unraid box's actual LAN IP,
`http://192.168.50.46:5001`) since a calling agent on some other LAN machine can't resolve
Compose's internal DNS. Conflating the two would have shipped a tool that returns URLs that
work from inside the container and nowhere else.

**No auth**, matching the existing Flask app's posture — same trusted-home-LAN threat model
as the web UI, nothing new introduced here.

**Fourth tool added post-hoc: `list_checkpoints`.** `comfy_client.list_checkpoints()` already
existed and was already called by `server.py`'s worker, but nothing exposed it over HTTP.
Added a `GET /checkpoints` route (thin wrapper around the same function, 502 on a ComfyUI-
reachability failure) and a matching MCP tool. Informational only for now — `generate_image`
doesn't accept a checkpoint override, `llm_bridge.py` always auto-picks one from the prompt —
so the docstring says so explicitly rather than implying a control the tool doesn't have.

Verified locally via `docker compose up -d --build` and a real MCP client (Streamable HTTP)
driving the live tools end to end: clarify path, generate -> poll -> done -> image_url loads,
gallery listing, and the unknown-job-id / service-unreachable error paths all return clean
structured messages rather than stack traces. Cross-machine verification (confirming
`image_url` loads from a LAN machine other than Unraid) deferred to actual Unraid deployment.

**Bug found during that verification:** `check_image_status` initially used a generic
`"error" in result` check to detect a failed HTTP call to the Flask API. But `/status/<job_id>`
always includes an `"error"` key in its body as normal domain data (`null` for a healthy job,
a message for a genuinely failed one) — so every in-progress job was misreported as
`{"status": "error", "error": None}`. Fixed by switching the transport-failure signal to a
dedicated `_transport_error` key that can't collide with a real API response's own fields.

**Discovered while testing the public image URL:** hitting `192.168.50.46:5001` (the intended
Unraid deploy target) from outside this app returned an unrelated Express service — something
else already listens there. In response, made both ports configurable rather than hardcoded:
`server.py` now reads `PORT` (default `5001`) via `config.py`, and `mcp_server/server.py`
already read `MCP_PORT` (default `8000`). `docker-compose.yml` uses `${PORT:-5001}` /
`${MCP_PORT:-8000}` for both the container env var and the host port mapping, sourced from
`nl-image-gen/.env` (Compose auto-loads that file for variable substitution — the same file
`config.py` already loads for everything else). Changing one line in `.env` now moves either
service off a conflicting port everywhere it's referenced (host mapping, internal
`NL_IMAGE_GEN_URL`, and `NL_IMAGE_GEN_PUBLIC_URL`) instead of needing edits in multiple files.

## Session 5: qwen3:8b thinking-mode timeout fix

Live-tested a real generation from the web UI with a long, detailed prompt (a kids' bike with
training wheels, specific colors, a basket, lightning-streak effects). It failed with `/api/chat`
returning `Read timed out. (read timeout=120)` from Ollama — the job never even got to
submitting a ComfyUI workflow.

Root cause: `qwen3:8b` is a reasoning ("thinking") model. By default it emits a `<think>...</think>`
chain-of-thought block before its actual answer, even with `format: "json"` requested.
`llm_bridge.py`'s `build_spec()`/`assess_ambiguity()` never disabled this. Confirmed directly
against the live Ollama instance (`192.168.50.46:11434`): replaying the exact failing request
with no changes reliably exceeded a 150s timeout, while adding `"think": false` to the request
body returned a valid JSON spec in 3.8s. Not a VRAM/hardware limit — both `qwen3:8b` and
`qwen2.5:7b` are already pulled locally at ~5GB (Q4_K_M) each, comfortably within an RTX 3060.

Fix: added `"think": False` to both Ollama request payloads in `llm_bridge.py` (`build_spec()`
and `assess_ambiguity()`). Chosen over swapping to `qwen2.5:7b` (the other option considered)
because it's a one-line change per call with no model swap, and this pipeline's spec-extraction
task doesn't benefit from reasoning depth anyway — it's filling in a fixed JSON schema, not
solving anything novel. Verified by resubmitting the exact prompt that had timed out: it now
reaches ComfyUI and completes normally (`nl_gen_00029_.png`, 25/25 steps, image loads).

## Session 6: LoRA library

Picked up the queued LoRA follow-up from Session 5. Verified real CivitAI download URLs via
their API (never guessed model IDs, same discipline as the ChimeraMi/Juggernaut checkpoint
picks) and downloaded three SDXL 1.0 LoRAs into `models/loras/` on the local ComfyUI install:

- `add-detail-xl.safetensors` — "Detail Tweaker XL" (civitai.com/models/122359), general
  detail/texture booster, ungated download.
- `CinematicStyle_v1.safetensors` — "Cinematic Shot✨" SDXL version (civitai.com/models/432586),
  film-look lighting/color grading, creator-gated (needed a CivitAI API key — used the one
  already configured in the sibling `comfyui-manager` project's `download-models.sh`, which
  handles both the model-page-URL and gated-download cases).
- `anime-enhancer-xl.safetensors` — originally downloaded as `enhancerV4-xl.safetensors`
  ("Anime Enhancer" civitai.com/models/348852, SDXL V4), creator-gated. **Renamed on disk**
  after the first live test: the LLM was picking it for a plain "highly detailed" prompt
  instead of the actual detail-tweaker LoRA, because `enhancerV4-xl` doesn't say "anime"
  anywhere and the model latched onto "enhancer" generically. A self-describing filename
  fixed it outright — cheaper and more robust than hardcoding a name→description lookup
  table in the prompt, and it stays correct for any LoRA added later without code changes.

Wired end-to-end:
- `comfy_client.list_loras()` already existed (hits ComfyUI's `/models/loras`), just wasn't
  used anywhere yet.
- `llm_bridge.SYSTEM_PROMPT` gained an `Available LoRAs: {loras}` line and a `"loras"` output
  field (0-2 objects, `{"name", "strength"}`) with guidance tying each LoRA's filename keyword
  to when the LLM should pick it. `build_spec()` now takes a `loras` list, validates each
  returned entry against it (unknown names dropped, strength clamped to `[0.0, 2.0]`, capped
  at 2 entries) — fails safe to an empty list rather than erroring.
- `workflow_builder.build_workflow()` threads a `LoraLoader` chain between the
  `CheckpointLoaderSimple` node and every downstream model/clip consumer (`CLIPTextEncode`,
  both `KSampler`s, `FaceDetailer`) — composes with the existing hires/face_fix passes.
- Added `/loras` route (`server.py`, mirrors `/checkpoints`) and `list_loras` MCP tool
  (`mcp_server/server.py`, mirrors `list_checkpoints`) for visibility/parity, though nothing
  currently lets a caller override the LLM's pick (same pattern as checkpoints).
- `server.py`'s worker fetches LoRAs alongside checkpoints before calling `build_spec()`;
  wrapped in try/except so a ComfyUI LoRA-listing hiccup never blocks generation (checkpoints
  stay a hard requirement, LoRAs stay optional).

Verified live end-to-end inside the running container (real Ollama calls, real ComfyUI
submission, real 8188 host, no mocking): a "highly detailed, intricate" prompt reliably picked
`add-detail-xl` alone; an anime-themed prompt picked `anime-enhancer-xl` alone; a plain prompt
picked no LoRAs (LLM doesn't stack speculatively); a "cinematic, highly detailed" prompt
rendered through a real `LoraLoader` chain end to end (`nl_gen_00035_.png`) — visibly more
dramatic/film-like lighting than the base checkpoint alone.

## Open items (not started)

- Unraid deployment itself hasn't happened yet — everything has been tested via
  `docker compose` on the Mac, against the real LAN ComfyUI/Ollama hosts. The app is designed
  to be host-agnostic (see Session 2), so moving the container to the actual Unraid box
  should be a non-event, but hasn't been verified in practice.
