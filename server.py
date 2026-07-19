#!/usr/bin/env python3
"""Flask web server for natural-language image generation.

Serves a single-page Vue-CDN UI, runs generation jobs on a background worker
thread (serializing them against the shared GPU-backed ComfyUI instance),
tracks a small SQLite gallery history, and proxies generated images from
ComfyUI's /view endpoint so the app never needs local filesystem access.
"""
import queue
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request
from flask_login import login_required

import comfy_client
import llm_bridge
from auth import current_actor, init_auth, require_auth
from config import COMFY_URL, GALLERY_DB_PATH, PORT, SECRET_KEY
from workflow_builder import build_workflow

app = Flask(__name__)
app.secret_key = SECRET_KEY

# index.html is a self-contained Vue app whose `{{ }}` interpolation syntax
# collides with Jinja2's own `{{ }}` templating. We never need server-side
# templating for it, so serve it as a raw static file instead of routing it
# through render_template (which would make Jinja try, and fail, to resolve
# Vue expressions like `{{ item.prompt }}` server-side).
_INDEX_HTML_PATH = Path(__file__).parent / "templates" / "index.html"
_LOGIN_HTML_PATH = Path(__file__).parent / "templates" / "login.html"

init_auth(app, _LOGIN_HTML_PATH)

# --- Job queue / worker -----------------------------------------------------

job_queue: "queue.Queue[str]" = queue.Queue()
jobs: dict = {}
jobs_lock = threading.Lock()

# Bare filename ComfyUI would produce via SaveImage, e.g. nl_gen_00007_.png
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(png|jpg|jpeg|webp)$", re.IGNORECASE)


def _set_job(job_id: str, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def worker():
    while True:
        job_id = job_queue.get()
        try:
            with jobs_lock:
                prompt = jobs[job_id]["prompt"]
                user_id = jobs[job_id]["user_id"]
                origin = jobs[job_id]["origin"]
            _set_job(job_id, status="running")

            checkpoints = comfy_client.list_checkpoints()
            if not checkpoints:
                raise RuntimeError("No checkpoints found on ComfyUI server.")
            try:
                loras = comfy_client.list_loras()
            except Exception:  # noqa: BLE001 - LoRAs are optional, never block generation
                loras = []

            spec = llm_bridge.build_spec(prompt, checkpoints, loras)
            workflow, save_node_id = build_workflow(spec)
            prompt_id = comfy_client.submit_workflow(workflow, client_id=job_id)

            def on_progress(step, total_steps):
                _set_job(job_id, step=step, total_steps=total_steps)

            try:
                image = comfy_client.wait_for_result_ws(
                    prompt_id, client_id=job_id, save_node_id=save_node_id,
                    on_progress=on_progress,
                )
            except Exception:
                # websocket path failed for a reason unrelated to generation
                # (e.g. connection drop) -- fall back to HTTP polling so the
                # job still completes.
                image = comfy_client.wait_for_result(prompt_id, save_node_id=save_node_id)
            filename = image["filename"]

            _set_job(job_id, status="done", filename=filename, error=None)
            _record_generation(job_id, prompt, filename, user_id, origin)
        except Exception as e:  # noqa: BLE001 - worker must never die
            _set_job(job_id, status="error", error=str(e))
        finally:
            job_queue.task_done()


# --- SQLite gallery history --------------------------------------------------


def init_db():
    conn = sqlite3.connect(GALLERY_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS generations (
                job_id TEXT PRIMARY KEY,
                prompt TEXT,
                filename TEXT,
                timestamp TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE,
                email         TEXT UNIQUE,
                password_hash TEXT,
                oauth_provider TEXT,
                oauth_sub     TEXT,
                created_at    TEXT
            )
            """
        )

        # Idempotent migration: add user_id/origin to generations if this DB
        # predates auth. origin backfill only runs the moment the column is
        # created, so pre-auth rows are classified as community exactly once
        # and later startups don't re-run it.
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(generations)").fetchall()
        }
        if "user_id" not in existing_cols:
            conn.execute("ALTER TABLE generations ADD COLUMN user_id INTEGER")
        if "origin" not in existing_cols:
            conn.execute("ALTER TABLE generations ADD COLUMN origin TEXT DEFAULT 'user'")
            conn.execute(
                "UPDATE generations SET origin='community' WHERE origin IS NULL OR origin='user'"
            )

        conn.commit()
    finally:
        conn.close()


def _record_generation(job_id: str, prompt: str, filename: str, user_id, origin: str):
    timestamp = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(GALLERY_DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO generations (job_id, prompt, filename, timestamp, user_id, origin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, prompt, filename, timestamp, user_id, origin),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_gallery(user_id, limit: int = 50) -> list:
    conn = sqlite3.connect(GALLERY_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT job_id, prompt, filename, timestamp, origin FROM generations "
            "WHERE user_id = ? OR origin = 'community' ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# --- Routes -------------------------------------------------------------


@app.route("/")
@login_required
def index():
    return Response(_INDEX_HTML_PATH.read_text(), mimetype="text/html")


@app.route("/generate", methods=["POST"])
@require_auth
def generate_route():
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    clarified = bool(body.get("clarified"))

    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    if not clarified:
        assessment = llm_bridge.assess_ambiguity(prompt)
        if assessment.get("needs_clarification"):
            return jsonify({"status": "clarify", "question": assessment.get("question")})

    job_id = uuid.uuid4().hex
    user_id, origin = current_actor()
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "prompt": prompt,
            "filename": None,
            "error": None,
            "step": None,
            "total_steps": None,
            "user_id": user_id,
            "origin": origin,
        }
    job_queue.put(job_id)
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
@require_auth
def status_route(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job_id"}), 404
        return jsonify(dict(job))


@app.route("/gallery")
@require_auth
def gallery_route():
    user_id, _ = current_actor()
    return jsonify(_fetch_gallery(user_id))


@app.route("/checkpoints")
@require_auth
def checkpoints_route():
    try:
        checkpoints = comfy_client.list_checkpoints()
    except Exception as e:  # noqa: BLE001 - surface any ComfyUI-reachability issue uniformly
        return jsonify({"error": str(e)}), 502
    return jsonify(checkpoints)


@app.route("/loras")
@require_auth
def loras_route():
    try:
        loras = comfy_client.list_loras()
    except Exception as e:  # noqa: BLE001 - surface any ComfyUI-reachability issue uniformly
        return jsonify({"error": str(e)}), 502
    return jsonify(loras)


@app.route("/health")
def health_route():
    # Intentionally unauthenticated: a health check must be cheaply reachable
    # for infra/agent preflighting without credentials, and MCP_API_KEY is
    # unset by default in this repo, which would make an authed route unusable
    # by the MCP server itself.
    services = {}
    for name, check in (("comfyui", comfy_client.check_alive), ("ollama", llm_bridge.check_alive)):
        t0 = time.perf_counter()
        try:
            check()
            services[name] = {"reachable": True, "latency_ms": round((time.perf_counter() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001 - reachability probe, any failure means "down"
            services[name] = {"reachable": False, "error": str(e)}

    overall = "ok" if all(s["reachable"] for s in services.values()) else "degraded"
    return jsonify({"status": overall, "services": services})


@app.route("/image/<name>")
@require_auth
def image_route(name):
    if not _FILENAME_RE.match(name):
        return jsonify({"error": "invalid filename"}), 400

    upstream = requests.get(
        f"{COMFY_URL}/view",
        params={"filename": name, "type": "output"},
        stream=True,
        timeout=30,
    )
    if upstream.status_code != 200:
        return jsonify({"error": "image not found"}), 404

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    return Response(upstream.iter_content(chunk_size=8192), content_type=content_type)


# --- Startup -------------------------------------------------------------

init_db()
threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
