#!/usr/bin/env python3
import sys
import comfy_client
import llm_bridge
from workflow_builder import build_workflow


def generate(user_prompt: str) -> str:
    checkpoints = comfy_client.list_checkpoints()
    if not checkpoints:
        raise SystemExit("No checkpoints found on ComfyUI server.")

    print(f"[1/4] Parsing prompt via LLM...")
    spec = llm_bridge.build_spec(user_prompt, checkpoints)
    print(f"      prompt: {spec['positive_prompt']}")
    print(f"      checkpoint: {spec['checkpoint']}  steps: {spec['steps']}  cfg: {spec['cfg']}  sampler: {spec['sampler_name']}  hires: {spec.get('hires', False)}  face_fix: {spec.get('face_fix', False)}")

    print(f"[2/4] Building workflow...")
    workflow, save_node_id = build_workflow(spec)

    print(f"[3/4] Submitting to ComfyUI...")
    prompt_id = comfy_client.submit_workflow(workflow)
    print(f"      prompt_id: {prompt_id}")

    print(f"[4/4] Waiting for image...")
    image = comfy_client.wait_for_result(prompt_id, save_node_id=save_node_id)
    filename = image["filename"]
    subfolder = image.get("subfolder", "")
    print(f"\nDone: {filename} (subfolder={subfolder})")
    return filename


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 generate.py \"your image description\"")
        sys.exit(1)

    prompt = " ".join(sys.argv[1:])
    generate(prompt)
