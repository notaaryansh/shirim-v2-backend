"""Clone + path-safety helpers for agent workdirs."""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

INSTALLS_DIR = Path("~/.shirim/installs").expanduser()
INSTALLS_DIR.mkdir(parents=True, exist_ok=True)

# Dirs the repo-walker + tools should never descend into.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".shirim-venv",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
    ".shirim-gopath",
    ".shirim-gocache",
    ".shirim-cargo-home",
    ".shirim-target",
    ".shirim-npm-prefix",
}


def safe_path(workdir: Path, rel: str) -> Path | None:
    """Resolve `rel` inside `workdir` and reject any path traversal."""
    try:
        full = (workdir / rel).resolve()
        workdir_abs = workdir.resolve()
    except Exception:
        return None
    if full == workdir_abs:
        return full
    try:
        full.relative_to(workdir_abs)
    except ValueError:
        return None
    return full


def walk_repo_tree(workdir: Path, max_files: int = 600) -> list[str]:
    """Return a sorted list of relative file paths, skipping SKIP_DIRS and dotfiles."""
    results: list[str] = []
    for path in sorted(workdir.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(workdir).parts
        if any(p in SKIP_DIRS for p in rel_parts):
            continue
        # Allow dot-prefixed root files (.env.example, .gitignore) but skip
        # nested hidden dirs (except those already covered by SKIP_DIRS).
        if any(p.startswith(".") and p not in (".env", ".env.example", ".env.sample") and i > 0
               for i, p in enumerate(rel_parts)):
            continue
        results.append("/".join(rel_parts))
        if len(results) >= max_files:
            break
    return results


async def clone_repo(owner: str, repo: str, workdir: Path, ref: str | None = None) -> None:
    """Shallow-clone github.com/{owner}/{repo} into workdir. Raises on failure."""
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    args = ["git", "clone", "--depth=1"]
    if ref:
        args += ["--branch", ref]
    args += [url, str(workdir)]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git clone failed for {owner}/{repo}: {stderr.decode(errors='replace')}"
        )
    log.info("cloned %s/%s into %s", owner, repo, workdir)


def cleanup_install(install_id: str) -> bool:
    """Delete the workdir for an install. Returns True if anything was removed."""
    workdir = INSTALLS_DIR / install_id
    if not workdir.exists():
        return False
    shutil.rmtree(workdir)
    return True
