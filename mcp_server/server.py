#!/usr/bin/env python3
"""MCP server exposing nl-image-gen's Flask API to LAN agents over Streamable HTTP.

Thin wrapper only: every tool here calls the existing Flask app (`/generate`,
`/status/<job_id>`, `/gallery`, `/checkpoints`) rather than talking to ComfyUI
directly, so the single-worker job queue in server.py keeps serializing all
generation against the shared GPU regardless of how many agents call in.
"""
import os

import requests
from mcp.server.fastmcp import FastMCP

# Where this server's own HTTP calls go -- typically the Docker Compose service
# name, only resolvable inside the compose network.
NL_IMAGE_GEN_URL = os.environ.get("NL_IMAGE_GEN_URL", "http://nl-image-gen:5001").rstrip("/")

# Base used to build image_url values handed back to calling agents, which run on
# other LAN machines and can't resolve the Compose service name above. Must be a
# LAN-reachable host:port. Defaults to NL_IMAGE_GEN_URL for local single-host testing.
NL_IMAGE_GEN_PUBLIC_URL = os.environ.get("NL_IMAGE_GEN_PUBLIC_URL", NL_IMAGE_GEN_URL).rstrip("/")

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
HTTP_TIMEOUT = 30

# Shared secret presented to the Flask app so its `require_auth` decorator lets
# this server's calls through even without a logged-in session.
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

mcp = FastMCP("nl-image-gen", host=MCP_HOST, port=MCP_PORT)


def _image_url(filename: str) -> str:
    return f"{NL_IMAGE_GEN_PUBLIC_URL}/image/{filename}"


def _request(method: str, path: str, **kwargs) -> dict:
    """Call the Flask API and return a structured transport-error dict on failure.

    Callers check for "_transport_error" rather than dealing with exceptions, so a
    stack trace never leaks out as a tool result. A plain "error" key is not used
    as the failure signal here because some successful Flask responses (e.g. a
    running job's /status body) legitimately include their own "error": null
    field as domain data, not a transport failure.
    """
    kwargs.setdefault("headers", {})
    kwargs["headers"]["X-API-Key"] = MCP_API_KEY

    try:
        resp = requests.request(method, f"{NL_IMAGE_GEN_URL}{path}", timeout=HTTP_TIMEOUT, **kwargs)
    except requests.exceptions.RequestException as e:
        return {"_transport_error": f"Could not reach the image generation service at {NL_IMAGE_GEN_URL}: {e}"}

    try:
        body = resp.json()
    except ValueError:
        body = {}

    if resp.status_code >= 400:
        message = body.get("error") if isinstance(body, dict) else None
        return {
            "_transport_error": message or f"Image service returned HTTP {resp.status_code}",
            "_status_code": resp.status_code,
        }

    return body


@mcp.tool()
def generate_image(prompt: str, clarified: bool = False) -> dict:
    """Generate an image from a natural-language description.

    Returns immediately with a job_id; generation runs asynchronously and takes
    roughly 15 seconds to 3 minutes. Poll check_image_status with the returned
    job_id to get progress and the final image URL.

    If the prompt is ambiguous, this returns {"status": "clarify", "question": "..."}
    with NO job_id -- no image is generated. Read the question, then call
    generate_image again with a more specific prompt and clarified=true to proceed.
    Set clarified=true up front to skip the ambiguity check entirely when you are
    already confident the prompt is specific.

    Returns one of:
      {"status": "queued", "job_id": str}
      {"status": "clarify", "question": str}
      {"status": "error", "error": str}
    """
    result = _request("POST", "/generate", json={"prompt": prompt, "clarified": clarified})

    if "_transport_error" in result:
        return {"status": "error", "error": result["_transport_error"]}
    if result.get("status") == "clarify":
        return {"status": "clarify", "question": result.get("question")}
    if "job_id" in result:
        return {"status": "queued", "job_id": result["job_id"]}
    return {"status": "error", "error": f"Unexpected response from image service: {result}"}


@mcp.tool()
def check_image_status(job_id: str) -> dict:
    """Check the status of an image generation job by its job_id (from generate_image).

    While running, returns progress as completed/total sampler steps. When
    finished, returns status="done" and image_url, a directly-loadable URL for the
    finished PNG -- no further calls needed. Returns status="error" if the job
    failed or the job_id is unknown (job state is kept in memory by the image
    service and is lost if it restarts).
    """
    result = _request("GET", f"/status/{job_id}")

    if "_transport_error" in result:
        if result.get("_status_code") == 404:
            return {
                "status": "error",
                "error": f"Unknown job_id '{job_id}'. Job state is kept in memory and is "
                "lost if the image service restarts.",
            }
        return {"status": "error", "error": result["_transport_error"]}

    status = result.get("status")
    prompt = result.get("prompt")

    if status == "done":
        return {
            "status": "done",
            "prompt": prompt,
            "filename": result.get("filename"),
            "image_url": _image_url(result.get("filename")),
        }

    if status == "error":
        return {"status": "error", "prompt": prompt, "error": result.get("error")}

    out = {"status": status, "prompt": prompt}
    step, total_steps = result.get("step"), result.get("total_steps")
    if step is not None and total_steps is not None:
        out["progress"] = f"{step}/{total_steps} steps"
    return out


@mcp.tool()
def list_recent_generations(limit: int = 10) -> list:
    """List the most recently generated images (default 10, max 50), newest first.

    Each entry includes the prompt, a directly-loadable image_url, and a
    timestamp. Use this to see what images already exist or to re-fetch a URL for
    an earlier generation whose job_id you no longer have (job_ids are forgotten
    when the image service restarts, but this gallery persists across restarts).
    """
    result = _request("GET", "/gallery")

    if isinstance(result, dict) and "_transport_error" in result:
        return [{"error": result["_transport_error"]}]

    limit = min(max(limit, 1), 50)
    return [
        {
            "job_id": row.get("job_id"),
            "prompt": row.get("prompt"),
            "image_url": _image_url(row.get("filename")),
            "timestamp": row.get("timestamp"),
        }
        for row in result[:limit]
    ]


@mcp.tool()
def list_checkpoints() -> dict:
    """List the ComfyUI checkpoint (model) files currently installed on the image
    generation server.

    Informational only: generate_image does not currently accept a checkpoint
    override, it automatically picks one based on the prompt (a fast/lightning
    checkpoint for quick requests, a general-purpose quality checkpoint otherwise).
    Use this to see what styles/capabilities are available, not to select one.

    Returns {"status": "ok", "checkpoints": [str, ...]} or
    {"status": "error", "error": str}.
    """
    result = _request("GET", "/checkpoints")

    if isinstance(result, dict) and "_transport_error" in result:
        return {"status": "error", "error": result["_transport_error"]}

    return {"status": "ok", "checkpoints": result}


@mcp.tool()
def cancel_image_generation(job_id: str) -> dict:
    """Cancel a queued or in-progress image generation job by its job_id.

    Use this to abort a job you no longer want (from generate_image). A job that
    is still queued is dropped before it runs; a job that is actively running is
    interrupted on the GPU. Cancelling a job that has already finished, failed,
    or was already cancelled is a harmless no-op (cancelled=false). Job state is
    kept in memory by the image service and is lost if it restarts, so an unknown
    job_id returns status="error".

    Returns one of:
      {"status": "cancelled", "cancelled": true}
      {"status": "done"|"error"|"cancelled", "cancelled": false, "message": str}
      {"status": "error", "error": str}
    """
    result = _request("POST", f"/cancel/{job_id}")

    if "_transport_error" in result:
        if result.get("_status_code") == 404:
            return {
                "status": "error",
                "error": f"Unknown job_id '{job_id}'. Job state is kept in memory and is "
                "lost if the image service restarts.",
            }
        return {"status": "error", "error": result["_transport_error"]}

    return result


@mcp.tool()
def list_loras() -> dict:
    """List the ComfyUI LoRA files currently installed on the image generation server.

    Informational only: generate_image does not currently accept a LoRA override,
    the LLM automatically picks 0-2 based on the prompt (e.g. a detail-enhancer for
    highly-detailed requests, a cinematic/anime style LoRA when the prompt calls for
    it). Use this to see what's available, not to select one.

    Returns {"status": "ok", "loras": [str, ...]} or
    {"status": "error", "error": str}.
    """
    result = _request("GET", "/loras")

    if isinstance(result, dict) and "_transport_error" in result:
        return {"status": "error", "error": result["_transport_error"]}

    return {"status": "ok", "loras": result}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
