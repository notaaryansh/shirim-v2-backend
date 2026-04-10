"""/api/search — GitHub repo search with library filtering + optional summary enrichment."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..classifier import classify_repo
from ..github import fetch_repo_and_readme, format_stars, search_repositories
from ..schemas import Repository, SmartSummary
from ..summarizer import get_or_build_summary

log = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


class SearchResponse(BaseModel):
    query: str
    total_count: int            # what GitHub reported (1 for URL-resolve)
    returned: int               # after classifier filtering
    filtered_out: int           # how many were dropped as libraries
    resolved_as: str             # "search" | "url" | "slug"
    repos: list[Repository]


# Match github.com/<owner>/<repo> with optional scheme, optional www,
# optional trailing path/query/fragment. Also handles git@github.com:owner/repo.git.
_GITHUB_URL_RE = re.compile(
    r"""
    ^\s*
    (?:                                         # one of:
        (?:https?://)?(?:www\.)?github\.com/    #   https://[www.]github.com/
        |
        git@github\.com:                        #   git@github.com:
    )
    (?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})   # owner — github rules
    /
    (?P<repo>[A-Za-z0-9_.-]+?)                  # repo name
    (?:\.git)?                                  # optional .git suffix
    (?:/.*)?                                    # optional trailing path
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Bare owner/repo slug — e.g. "sharkdp/bat"
_BARE_SLUG_RE = re.compile(
    r"^\s*(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/(?P<repo>[A-Za-z0-9_.-]+?)\s*$"
)


def _parse_github_ref(q: str) -> tuple[str, str, str] | None:
    """Try to interpret the query as a GitHub URL or bare slug.

    Returns (owner, repo_name, resolution_kind) or None if the query doesn't
    look like either. repo_name has any trailing `.git` stripped.
    """
    m = _GITHUB_URL_RE.match(q)
    if m:
        return m.group("owner"), m.group("repo"), "url"
    m = _BARE_SLUG_RE.match(q)
    if m:
        return m.group("owner"), m.group("repo"), "slug"
    return None


def _stable_id(full_name: str) -> int:
    # Same algorithm used by routes/repos.py — keeps React keys stable across
    # home/discover/search if the same repo appears in multiple places.
    h = 0
    for ch in full_name:
        h = (h * 131 + ord(ch)) & 0x7FFFFFFF
    return h


def _repo_from_search_item(item: dict) -> Repository:
    """Build a slim Repository from a GitHub search result. No summary."""
    full_name = item.get("full_name") or ""
    return Repository(
        id=_stable_id(full_name),
        name=item.get("name") or full_name.split("/", 1)[-1],
        repo=full_name,
        desc=(item.get("description") or "").strip(),
        language=(item.get("language") or "").strip(),
        stars=format_stars(int(item.get("stargazers_count") or 0)),
        summary=None,
    )


async def _enrich_one(item: dict) -> Repository | None:
    """Fetch README + generate/load summary for a single search result.

    Used when ?enrich=true. This is the slow path — each repo triggers a
    README fetch + possible OpenAI call. Failures are swallowed so one bad
    repo doesn't break the whole search response.
    """
    full_name = item.get("full_name") or ""
    try:
        owner, repo = full_name.split("/", 1)
    except ValueError:
        return _repo_from_search_item(item)

    try:
        repo_data, readme, _base = await fetch_repo_and_readme(owner, repo)
    except Exception as e:
        log.warning("enrich fetch failed for %s: %s", full_name, e)
        return _repo_from_search_item(item)

    if not repo_data:
        return _repo_from_search_item(item)

    try:
        summary_dict = await get_or_build_summary(
            owner=owner,
            repo=repo,
            readme=readme,
            repo_name=full_name,
            repo_desc=(repo_data.get("description") or "").strip(),
        )
    except Exception as e:
        log.warning("enrich summary failed for %s: %s", full_name, e)
        summary_dict = None

    slim = _repo_from_search_item(repo_data)
    if summary_dict:
        slim.summary = SmartSummary(**summary_dict)
    return slim


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=50),
    language: Optional[str] = Query(None, description="e.g. python, rust, go, typescript"),
    include_libraries: bool = Query(
        False, description="Set true to include library/SDK repos in the results"
    ),
    enrich: bool = Query(
        False,
        description="If true, fetch README + generate smart summary for each result. Slow.",
    ),
    user: User = Depends(get_current_user),
):
    """Search GitHub for repos matching `q`, filter libraries, return a slim result list.

    Special case: if `q` looks like a GitHub URL or bare `owner/repo` slug, this
    endpoint short-circuits the search API and returns that one repo directly.
    Library filtering is skipped on URL-resolve because the user explicitly
    asked for that repo by name.

    By default returns results without smart summaries for speed — the frontend
    should use /api/repos/{owner}/{repo} to lazy-load full details on card click.
    Pass enrich=true if you need summaries synchronously.
    """
    # ----- URL / slug short-circuit -----
    ref = _parse_github_ref(q)
    if ref is not None:
        owner, repo_name, kind = ref
        try:
            repo_data, readme, _base = await fetch_repo_and_readme(owner, repo_name)
        except Exception as e:
            log.warning("url-resolve fetch failed for %s/%s: %s", owner, repo_name, e)
            raise HTTPException(502, f"github fetch failed: {e}")

        if not repo_data:
            # Not found — return empty but with 200 + resolved_as so the
            # frontend can show "no such repo" instead of a search error.
            return SearchResponse(
                query=q,
                total_count=0,
                returned=0,
                filtered_out=0,
                resolved_as=kind,
                repos=[],
            )

        if enrich:
            enriched = await _enrich_one(repo_data)
            repos = [enriched] if enriched else []
        else:
            repos = [_repo_from_search_item(repo_data)]

        return SearchResponse(
            query=q,
            total_count=1,
            returned=len(repos),
            filtered_out=0,
            resolved_as=kind,
            repos=repos,
        )

    # ----- Regular keyword search path -----
    try:
        total, items = await search_repositories(q, limit=limit, language=language)
    except Exception as e:
        log.error("github search failed: %s", e)
        raise HTTPException(502, f"github search failed: {e}")

    # Classifier filter (default: drop libraries).
    kept_items: list[dict] = []
    filtered = 0
    for item in items:
        if not include_libraries:
            cls, score, reason = classify_repo(item)
            if cls == "library":
                filtered += 1
                log.info(
                    "search filtered library: %s (score=%d)", item.get("full_name"), score
                )
                continue
        kept_items.append(item)

    # Optional enrichment — do it in parallel.
    if enrich and kept_items:
        enriched = await asyncio.gather(*(_enrich_one(it) for it in kept_items))
        repos = [r for r in enriched if r is not None]
    else:
        repos = [_repo_from_search_item(it) for it in kept_items]

    return SearchResponse(
        query=q,
        total_count=total,
        returned=len(repos),
        filtered_out=filtered,
        resolved_as="search",
        repos=repos,
    )
