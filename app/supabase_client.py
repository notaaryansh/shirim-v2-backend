"""Singleton Supabase admin client. Ported from shirim/utils/supabase.py."""
from supabase import Client, create_client

from .config import SUPABASE_SECRET_KEY, SUPABASE_URL

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
            raise RuntimeError(
                "Supabase is not configured. Set SUPABASE_URL and "
                "SUPABASE_SECRET_KEY in .env."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client
