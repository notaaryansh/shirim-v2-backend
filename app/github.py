"""Async GitHub API helpers. Ported from shirim-v2/main.py."""
import base64
import re
from urllib.parse import urljoin

import httpx

from .config import GITHUB_API, GITHUB_HEADERS

MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_IMG_RE = re.compile(r"<img[^>]+src\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
BADGE_HOSTS = (
    "shields.io",
    "badge.fury.io",
    "travis-ci",
    "circleci",
    "codecov",
    "coveralls",
    "readthedocs",
    "pepy.tech",
    "badgen.net",
    "github.com/actions",
)


async def search_repositories(
    query: str,
    *,
    limit: int = 20,
    language: str | None = None,
) -> tuple[int, list[dict]]:
    """Search GitHub for repositories. Returns (total_count, items).

    `items` is a list of the raw GitHub search result dicts — same shape as
    /repos/{owner}/{repo} but slimmer (no topics unless requested, no license
    details, etc.). `total_count` is GitHub's reported total match count.
    """
    q = query.strip()
    if language:
        q = f"{q} language:{language.strip()}"
    params = {
        "q": q,
        "sort": "stars",
        "order": "desc",
        "per_page": max(1, min(limit, 100)),
    }
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.get(
            f"{GITHUB_API}/search/repositories",
            params=params,
            headers=GITHUB_HEADERS,
        )
        r.raise_for_status()
        data = r.json()
    return int(data.get("total_count") or 0), data.get("items") or []


async def fetch_repo_and_readme(
    owner: str, repo: str
) -> tuple[dict | None, str, str]:
    """Fetch repo metadata + README. Returns (None, "", "") if repo not found."""
    async with httpx.AsyncClient(timeout=20) as hc:
        rr = await hc.get(
            f"{GITHUB_API}/repos/{owner}/{repo}", headers=GITHUB_HEADERS
        )
        if rr.status_code == 404:
            return None, "", ""
        rr.raise_for_status()
        repo_data = rr.json()

        readme_text = ""
        readme_base = ""
        try:
            rd = await hc.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/readme", headers=GITHUB_HEADERS
            )
            if rd.status_code == 200:
                rd_json = rd.json()
                readme_text = base64.b64decode(
                    rd_json.get("content", "")
                ).decode("utf-8", errors="replace")
                readme_base = rd_json.get("download_url") or ""
        except Exception:
            pass

    return repo_data, readme_text, readme_base


async def fetch_repo_meta_only(owner: str, repo: str) -> dict | None:
    """Lighter fetch — skips README. Used when cached summary already exists."""
    async with httpx.AsyncClient(timeout=15) as hc:
        rr = await hc.get(
            f"{GITHUB_API}/repos/{owner}/{repo}", headers=GITHUB_HEADERS
        )
        if rr.status_code == 404:
            return None
        rr.raise_for_status()
        return rr.json()


def extract_images(md: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for m in MD_IMG_RE.finditer(md):
        urls.append(m.group(2))
    for m in HTML_IMG_RE.finditer(md):
        urls.append(m.group(1))

    resolved: list[str] = []
    seen: set[str] = set()
    for u in urls:
        u = u.strip()
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        elif not u.startswith(("http://", "https://")):
            if base_url:
                u = urljoin(base_url, u)
            else:
                continue
        if any(host in u for host in BADGE_HOSTS):
            continue
        if u in seen:
            continue
        seen.add(u)
        resolved.append(u)
    return resolved


def format_stars(n: int) -> str:
    """Format star count like the frontend: 118000 -> '118k'."""
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)
