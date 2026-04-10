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


CLONE_TIMEOUT_SECONDS = 90


async def clone_repo(
    owner: str,
    repo: str,
    workdir: Path,
    ref: str | None = None,
    timeout: float = CLONE_TIMEOUT_SECONDS,
) -> None:
    """Shallow-clone github.com/{owner}/{repo} into workdir.

    Raises on non-zero exit, on timeout (after killing the subprocess), or
    if the task is cancelled.
    """
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    # --no-tags and GIT_LFS_SKIP_SMUDGE=1 keep slow-path LFS pulls and
    # historical tags from blowing up clone time on large repos.
    args = [
        "git",
        "-c", "http.postBuffer=524288000",
        "clone",
        "--depth=1",
        "--no-tags",
        "--single-branch",
    ]
    if ref:
        args += ["--branch", ref]
    args += [url, str(workdir)]

    log.info("cloning %s/%s (timeout=%ds)", owner, repo, timeout)
    import os as _os
    env = _os.environ.copy()
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"  # don't block on credential prompts

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("clone timeout for %s/%s after %ds, killing git", owner, repo, timeout)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Drain so the proc's fds don't leak.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        # Best-effort cleanup of the partial clone.
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(
            f"git clone timed out after {timeout}s for {owner}/{repo}"
        )
    except asyncio.CancelledError:
        # Cooperative cancellation: kill the subprocess and re-raise so the
        # runner can record status=cancelled.
        log.info("clone cancelled for %s/%s", owner, repo)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
        raise

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
