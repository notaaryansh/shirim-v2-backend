import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..classifier import classify_repo
from ..curated import CURATED, DISCOVER_CATEGORIES, HOME_CATEGORIES
from ..github import (
    extract_images,
    fetch_repo_and_readme,
)
from ..schemas import CategoryBlock, RepoDetail, Repository, TabResponse
from ..summarizer import get_or_build_summary

log = logging.getLogger(__name__)
router = APIRouter()


def _stable_id(full_name: str) -> int:
    # Deterministic-per-process id for React keys. hash() is salted per-run in
    # Python 3.3+, which would change ids between server restarts — use a simple
    # stable hash instead.
    h = 0
    for ch in full_name:
        h = (h * 131 + ord(ch)) & 0x7FFFFFFF
    return h


async def build_repo(
    slug: str,
    *,
    include_summary: bool,
    include_libraries: bool = False,
) -> Repository | None:
    try:
        owner, repo = slug.split("/", 1)
    except ValueError:
        log.warning("bad slug: %r", slug)
        return None

    try:
        repo_data, readme, _readme_base = await fetch_repo_and_readme(owner, repo)
    except Exception as e:
        log.warning("github fetch failed for %s: %s", slug, e)
        return None

    if not repo_data:
        log.info("slug %s returned 404, skipping", slug)
        return None

    # Filter out libraries unless the caller opted in.
    cls, score, reason = classify_repo(repo_data)
    if cls == "library" and not include_libraries:
        log.info("filtered library: %s (score=%d, reason=%s)", slug, score, reason)
        return None

    from ..github import format_stars

    desc = (repo_data.get("description") or "").strip()
    language = (repo_data.get("language") or "").strip()
    stars_int = int(repo_data.get("stargazers_count") or 0)
    full_name = repo_data.get("full_name") or slug

    summary = None
    if include_summary:
        summary_dict = await get_or_build_summary(
            owner=owner,
            repo=repo,
            readme=readme,
            repo_name=full_name,
            repo_desc=desc,
        )
        summary = summary_dict

    return Repository(
        id=_stable_id(full_name),
        name=repo_data.get("name") or repo,
        repo=full_name,
        desc=desc,
        language=language,
        stars=format_stars(stars_int),
        summary=summary,
    )


async def _build_tab(
    tab: str,
    category_names: list[str],
    *,
    include_libraries: bool = False,
) -> TabResponse:
    blocks: list[CategoryBlock] = []
    for cat_name in category_names:
        slugs = CURATED.get(cat_name, [])
        repos_or_none = await asyncio.gather(
            *(
                build_repo(s, include_summary=True, include_libraries=include_libraries)
                for s in slugs
            )
        )
        blocks.append(
            CategoryBlock(
                name=cat_name,
                repos=[r for r in repos_or_none if r is not None],
            )
        )
    return TabResponse(tab=tab, categories=blocks)  # type: ignore[arg-type]


@router.get("/home", response_model=TabResponse)
async def get_home(
    include_libraries: bool = False,
    user: User = Depends(get_current_user),
):
    return await _build_tab(
        "home", HOME_CATEGORIES, include_libraries=include_libraries
    )


@router.get("/discover", response_model=TabResponse)
async def get_discover(
    include_libraries: bool = False,
    user: User = Depends(get_current_user),
):
    return await _build_tab(
        "discover", DISCOVER_CATEGORIES, include_libraries=include_libraries
    )


@router.get("/repos/{owner}/{repo}", response_model=RepoDetail)
async def get_repo_detail(
    owner: str,
    repo: str,
    user: User = Depends(get_current_user),
):
    try:
        repo_data, readme, readme_base = await fetch_repo_and_readme(owner, repo)
    except Exception as e:
        log.error("github fetch failed for %s/%s: %s", owner, repo, e)
        raise HTTPException(502, "github fetch failed")

    if not repo_data:
        raise HTTPException(404, "repo not found")

    from ..github import format_stars

    desc = (repo_data.get("description") or "").strip()
    full_name = repo_data.get("full_name") or f"{owner}/{repo}"

    summary_dict = await get_or_build_summary(
        owner=owner,
        repo=repo,
        readme=readme,
        repo_name=full_name,
        repo_desc=desc,
    )

    images = extract_images(readme, readme_base) if readme else []

    return RepoDetail(
        id=_stable_id(full_name),
        name=repo_data.get("name") or repo,
        repo=full_name,
        desc=desc,
        language=(repo_data.get("language") or "").strip(),
        stars=format_stars(int(repo_data.get("stargazers_count") or 0)),
        summary=summary_dict,  # type: ignore[arg-type]
        images=images,
        github_url=repo_data.get("html_url") or f"https://github.com/{full_name}",
    )
