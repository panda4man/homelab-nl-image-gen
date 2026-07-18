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

# --- Auth / sessions --------------------------------------------------------
# Flask session-cookie signing key. MUST be set (and kept secret) in any real
# deployment; the insecure fallback exists only so a fresh checkout boots.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

# Shared secret the MCP server presents via the X-API-Key header. Images it
# generates are stored unowned (user_id NULL) and tagged origin='ai'.
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

# Google OAuth (OIDC) credentials. Leave blank to disable the Google button.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# Public base URL the browser reaches this app at (e.g. a Tailscale hostname),
# used to build the Google OAuth callback URL. Blank -> derive from the request.
OAUTH_REDIRECT_BASE = os.environ.get("OAUTH_REDIRECT_BASE", "").rstrip("/")
