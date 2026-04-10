import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_CLIENT: OpenAI | None = OpenAI(api_key=_OPENAI_KEY) if _OPENAI_KEY else None

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS: dict[str, str] = {"Accept": "application/vnd.github+json"}
_gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
if _gh_token:
    GITHUB_HEADERS["Authorization"] = f"Bearer {_gh_token}"

BACKEND_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = BACKEND_ROOT / "cache" / "summaries"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

PORT = int(os.environ.get("SHIRIM_BACKEND_PORT", "8001"))

# --- Supabase auth ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
DEV_BYPASS_AUTH = os.environ.get("DEV_BYPASS_AUTH", "").lower() == "true"
DEV_USER_EMAIL = os.environ.get("DEV_USER_EMAIL", "").strip().lower()
