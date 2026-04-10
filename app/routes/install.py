"""/api/v1/install/* — agentic install loop endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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


class InstallRequest(BaseModel):
    ref: Optional[str] = None  # branch/tag, defaults to the repo's default branch


@router.post("/{owner}/{repo}")
async def start_install(
    owner: str,
    repo: str,
    body: InstallRequest | None = None,
    user: User = Depends(get_current_user),
):
    install_id = uuid.uuid4().hex[:12]
    run = AgentRun(
        install_id=install_id,
        owner=owner,
        repo=repo,
        ref=(body.ref if body else None),
    )
    _installs[install_id] = run
    # Fire and forget — the loop writes to run.logs/run.status.
    asyncio.create_task(run.run())
    return {"install_id": install_id}


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
            if run.status in ("success", "failure", "timeout", "error"):
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.delete("/{install_id}")
async def delete_install(install_id: str, user: User = Depends(get_current_user)):
    run = _installs.pop(install_id, None)
    removed = cleanup_install(install_id)
    if not run and not removed:
        raise HTTPException(404, "install not found")
    return {"ok": True, "removed": removed}
