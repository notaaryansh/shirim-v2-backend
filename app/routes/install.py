"""/api/v1/install/* — agentic install loop endpoints.

IMPORTANT: The POST /{owner}/{repo} wildcard route MUST be the LAST route
registered in this file. Otherwise it swallows requests meant for
/{install_id}/cancel, /{install_id}/progress, etc.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..agent.launcher import stop_runs_for_install
from ..agent.progress import compute_progress
from ..agent.runner import AgentRun
from ..agent.sandbox import INSTALLS_DIR, cleanup_install
from ..auth.dependencies import get_current_user
from ..auth.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/install", tags=["install"])

# In-memory registry of in-flight + recently-finished installs.
# Lost on process restart — that's an accepted v1 limitation.
_installs: dict[str, AgentRun] = {}
_tasks: dict[str, asyncio.Task] = {}  # install_id → background run task


def _is_terminal(run: AgentRun) -> bool:
    return run.status in ("success", "failure", "timeout", "cancelled", "error")


class InstallRequest(BaseModel):
    ref: Optional[str] = None  # branch/tag, defaults to the repo's default branch


# ---------- Specific routes FIRST (have literal path segments) ----------

@router.get("/{install_id}")
async def get_install(install_id: str, user: User = Depends(get_current_user)):
    run = _installs.get(install_id)
    if not run:
        raise HTTPException(404, "install not found")
    return {
        "install_id": install_id,
        "owner": run.owner,
        "repo": run.repo,
        "status": run.status,
        "phase": run.phase,
        "analysis": run.analysis,
        "result": run.result,
        "log_count": len(run.logs),
        "duration_ms": int(
            ((run.finished_at or 0) - (run.started_at or 0)) * 1000
        )
        if run.started_at
        else 0,
    }


@router.get("/{install_id}/analysis")
async def get_install_analysis(install_id: str, user: User = Depends(get_current_user)):
    run = _installs.get(install_id)
    if not run:
        raise HTTPException(404, "install not found")
    if run.analysis is None:
        raise HTTPException(202, "analysis not ready yet")
    return run.analysis


@router.get("/{install_id}/progress")
async def get_install_progress(install_id: str, user: User = Depends(get_current_user)):
    run = _installs.get(install_id)
    if not run:
        raise HTTPException(404, "install not found")
    return compute_progress(run)


@router.get("/{install_id}/stream")
async def stream_install(install_id: str, user: User = Depends(get_current_user)):
    run = _installs.get(install_id)
    if not run:
        raise HTTPException(404, "install not found")

    async def gen():
        idx = 0
        while True:
            n = len(run.logs)
            while idx < n:
                yield f"data: {json.dumps(run.logs[idx], default=str)}\n\n"
                idx += 1
            if run.status in ("success", "failure", "timeout", "cancelled", "error"):
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/{install_id}/cancel")
async def cancel_install(install_id: str, user: User = Depends(get_current_user)):
    """Soft cancel — sets the runner's cancel flag and cancels the asyncio Task.

    Keeps the workdir around for post-mortem inspection. Use DELETE if you
    also want the files wiped.
    """
    run = _installs.get(install_id)
    if not run:
        raise HTTPException(404, "install not found")
    if _is_terminal(run):
        return {"ok": True, "already_terminal": True, "status": run.status}

    run.request_cancel()
    task = _tasks.get(install_id)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    return {"ok": True, "status": run.status}


@router.delete("/{install_id}")
async def delete_install(install_id: str, user: User = Depends(get_current_user)):
    """Cancel (if running), stop any active launched process, then wipe the workdir."""
    run = _installs.pop(install_id, None)
    task = _tasks.pop(install_id, None)

    if run and not _is_terminal(run):
        run.request_cancel()
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    # Stop any live launched subprocess before we rm the workdir out from under it.
    stopped_runs = await asyncio.to_thread(stop_runs_for_install, install_id)
    if stopped_runs:
        log.info("stopped %d running process(es) for %s", stopped_runs, install_id)

    removed = cleanup_install(install_id)
    if not run and not removed:
        raise HTTPException(404, "install not found")
    return {"ok": True, "removed": removed, "stopped_runs": stopped_runs}


# ---------- Wildcard route LAST (catches any two-segment path) ----------

@router.post("/{owner}/{repo}")
async def start_install(
    owner: str,
    repo: str,
    body: InstallRequest | None = None,
    user: User = Depends(get_current_user),
):
    """Start an install. This route MUST be last in the file — it matches any
    POST to /{x}/{y} and would swallow /cancel, /run, etc. if registered first.
    """
    # Dedup: if the same user already has a non-terminal install for this
    # exact repo, return the existing install_id.
    for existing_id, existing in _installs.items():
        if (
            existing.owner == owner
            and existing.repo == repo
            and not _is_terminal(existing)
        ):
            log.info(
                "dedup: returning existing install %s for %s/%s",
                existing_id, owner, repo,
            )
            return {"install_id": existing_id, "deduped": True}

    install_id = uuid.uuid4().hex[:12]
    run = AgentRun(
        install_id=install_id,
        owner=owner,
        repo=repo,
        ref=(body.ref if body else None),
    )
    _installs[install_id] = run
    _tasks[install_id] = asyncio.create_task(run.run())
    return {"install_id": install_id}
