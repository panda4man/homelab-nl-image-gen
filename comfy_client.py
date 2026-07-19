import json
import time
import requests
import websocket
from config import COMFY_URL

WS_URL = COMFY_URL.replace("http://", "ws://").replace("https://", "wss://")


class ComfyError(Exception):
    pass


def check_alive(timeout=5) -> dict:
    r = requests.get(f"{COMFY_URL}/system_stats", timeout=timeout)
    r.raise_for_status()
    return r.json()


def list_checkpoints() -> list[str]:
    r = requests.get(f"{COMFY_URL}/models/checkpoints", timeout=10)
    r.raise_for_status()
    return r.json()


def list_loras() -> list[str]:
    r = requests.get(f"{COMFY_URL}/models/loras", timeout=10)
    r.raise_for_status()
    return r.json()


def interrupt(timeout=10) -> None:
    """Interrupt whatever prompt ComfyUI is currently executing.

    ComfyUI's /interrupt with no body does a *global* interrupt of the one
    prompt on the GPU right now. Because this app submits prompts strictly one
    at a time (single worker), that is always the job we mean to cancel -- no
    prompt_id targeting needed. Returns nothing; ComfyUI replies 200 with an
    empty body. Raises for any non-2xx (surfaced by the caller).
    """
    r = requests.post(f"{COMFY_URL}/interrupt", timeout=timeout)
    r.raise_for_status()


def submit_workflow(workflow: dict, client_id: str | None = None) -> str:
    body = {"prompt": workflow}
    if client_id:
        body["client_id"] = client_id
    r = requests.post(f"{COMFY_URL}/prompt", json=body)
    try:
        result = r.json()
    except ValueError:
        raise ComfyError(f"Non-JSON response ({r.status_code}): {r.text[:500]}")

    if r.status_code != 200:
        raise ComfyError(f"API error ({r.status_code}): {result}")

    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise ComfyError(f"No prompt_id returned: {result}")
    return prompt_id


def wait_for_result(prompt_id: str, save_node_id: str = "7", max_wait=180, poll_interval=2) -> dict:
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        r = requests.get(f"{COMFY_URL}/history/{prompt_id}")
        r.raise_for_status()
        history = r.json()

        if prompt_id not in history:
            continue

        entry = history[prompt_id]

        status = entry.get("status", {})
        if status.get("status_str") == "error":
            raise ComfyError(f"Generation failed: {status}")

        outputs = entry.get("outputs", {})
        if save_node_id in outputs and "images" in outputs[save_node_id]:
            return outputs[save_node_id]["images"][0]

    raise ComfyError(f"Timeout after {max_wait}s waiting for prompt {prompt_id}")


def _fetch_output(prompt_id: str, save_node_id: str) -> dict:
    r = requests.get(f"{COMFY_URL}/history/{prompt_id}")
    r.raise_for_status()
    entry = r.json().get(prompt_id, {})
    outputs = entry.get("outputs", {})
    if save_node_id in outputs and "images" in outputs[save_node_id]:
        return outputs[save_node_id]["images"][0]
    raise ComfyError(f"No output found on save node {save_node_id} for prompt {prompt_id}")


def wait_for_result_ws(prompt_id: str, client_id: str, save_node_id: str = "7",
                        on_progress=None, max_wait: int = 300) -> dict:
    """Waits for a queued prompt via ComfyUI's websocket API instead of HTTP polling,
    giving live per-step progress. `on_progress(step, total)` is called as KSampler
    steps complete. Raises ComfyError on generation failure or timeout; callers should
    fall back to wait_for_result() (HTTP polling) if the websocket connection itself
    fails, since that's a network-layer issue independent of generation success."""
    ws = websocket.create_connection(f"{WS_URL}/ws?clientId={client_id}", timeout=max_wait)
    try:
        ws.settimeout(max_wait)
        while True:
            msg = ws.recv()
            if isinstance(msg, bytes):
                continue
            data = json.loads(msg)
            mtype = data.get("type")
            payload = data.get("data", {})
            if payload.get("prompt_id") not in (None, prompt_id):
                continue

            if mtype == "progress" and on_progress:
                on_progress(payload.get("value", 0), payload.get("max", 1))
            elif mtype == "execution_error" and payload.get("prompt_id") == prompt_id:
                raise ComfyError(f"Generation failed: {payload}")
            elif mtype == "execution_interrupted" and payload.get("prompt_id") == prompt_id:
                raise ComfyError(f"Generation interrupted: {payload}")
            elif mtype == "executing" and payload.get("prompt_id") == prompt_id and payload.get("node") is None:
                break
    finally:
        ws.close()

    return _fetch_output(prompt_id, save_node_id)
