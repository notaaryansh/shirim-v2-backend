"""OpenAI-powered smart summaries with disk cache."""
import asyncio
import json
import logging

from .config import CACHE_DIR, OPENAI_CLIENT

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are extracting structured product info from a GitHub README so it "
    "can be displayed on an app-store-style page for non-technical users. "
    "Return ONLY valid JSON matching this schema:\n"
    "{\n"
    '  "tagline": string (<= 80 chars, catchy one-liner),\n'
    '  "description": string (2-3 sentences, plain language, no code),\n'
    '  "features": string[] (3-7 short bullet points, each <= 80 chars),\n'
    '  "categories": string[] (1-3 single-word categories like "Finance", "Productivity"),\n'
    '  "install_difficulty": "easy" | "medium" | "hard",\n'
    '  "requirements": string[] (main runtime requirements, e.g. "Python 3.10+", "Docker", "OpenAI API key")\n'
    "}\n"
    "Guidelines:\n"
    "- If README is thin, infer from name/description.\n"
    "- 'easy' = pip/npm install and run; 'medium' = config/env vars needed; 'hard' = system deps, build steps, or manual setup.\n"
    "- Avoid jargon in the description and tagline."
)


def _cache_path(owner: str, repo: str):
    safe = f"{owner}__{repo}".replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _fallback(repo_desc: str) -> dict:
    return {
        "tagline": (repo_desc or "")[:80],
        "description": repo_desc or "",
        "features": [],
        "categories": [],
        "install_difficulty": "medium",
        "requirements": [],
    }


def _call_openai_sync(readme: str, repo_name: str, repo_desc: str) -> dict:
    assert OPENAI_CLIENT is not None
    user = (
        f"Repo: {repo_name}\n"
        f"Short description: {repo_desc}\n\n"
        f"README:\n{readme[:12000]}"
    )
    resp = OPENAI_CLIENT.chat.completions.create(
        model="gpt-5.4-nano",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    return json.loads(resp.choices[0].message.content)


async def get_or_build_summary(
    owner: str,
    repo: str,
    readme: str,
    repo_name: str,
    repo_desc: str,
) -> dict:
    """Return a summary dict, using cache if available. Never raises."""
    path = _cache_path(owner, repo)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log.warning("corrupted cache file %s, regenerating", path)

    if OPENAI_CLIENT is None:
        log.warning("OPENAI_API_KEY not set — returning fallback summary")
        return _fallback(repo_desc)

    try:
        data = await asyncio.to_thread(
            _call_openai_sync, readme, repo_name, repo_desc
        )
    except Exception as e:
        log.error("openai summary failed for %s/%s: %s", owner, repo, e)
        return _fallback(repo_desc)

    # normalise — make sure all keys exist so Pydantic validation doesn't trip.
    fallback = _fallback(repo_desc)
    for k, v in fallback.items():
        data.setdefault(k, v)

    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.warning("failed to write summary cache %s: %s", path, e)

    return data
