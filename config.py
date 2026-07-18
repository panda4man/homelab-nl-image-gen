import os
from pathlib import Path

def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

_load_env()

PORT = int(os.environ.get("PORT", "5001"))

COMFY_HOST = os.environ.get("COMFY_HOST", "192.168.50.121")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"

LLM_HOST = os.environ.get("LLM_HOST", "192.168.50.46")
LLM_PORT = int(os.environ.get("LLM_PORT", "11434"))
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
LLM_URL = f"http://{LLM_HOST}:{LLM_PORT}"

EMBED_HOST = os.environ.get("EMBED_HOST", "192.168.50.46")
EMBED_PORT = int(os.environ.get("EMBED_PORT", "11435"))
EMBED_URL = f"http://{EMBED_HOST}:{EMBED_PORT}"

GALLERY_DB_PATH = os.environ.get("GALLERY_DB_PATH", str(Path(__file__).parent / "gallery.db"))
