"""/api/v1/install/{install_id}/run — launch installed repos as long-lived processes.

This module is independent of the in-memory _installs dict in routes/install.py —
it reads install.json straight from disk, so it survives uvicorn restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import vault
from ..agent.adapters import get_adapter
from ..agent.launcher import (
    RunHandle,
    get_run_for_install,
    normalise_command,
    start_run,
    stop_run,
)
from ..agent.sandbox import INSTALLS_DIR
from ..auth.dependencies import get_current_user
from ..auth.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/install", tags=["run"])


class RunRequest(BaseModel):
    command: Optional[str] = None  # override the run_command stored in install.json
    wait_for_url: Optional[float] = None  # override the 30s URL-detect window


class RunResponse(BaseModel):
    run_id: str
    install_id: str
    command: str
    pid: int
    url: Optional[str]
    port: Optional[int]
    status: str
    started_at: float
    finished_at: Optional[float]
    exit_code: Optional[int]


def _serialise(h: RunHandle) -> RunResponse:
    return RunResponse(
        run_id=h.run_id,
        install_id=h.install_id,
        command=h.command,
        pid=h.pid,
        url=h.url,
        port=h.port,
        status=h.status,
        started_at=h.started_at,
        finished_at=h.finished_at,
        exit_code=h.exit_code,
    )


def _load_install_json(install_id: str) -> dict:
    """Read install.json from disk. Raises HTTPException on any problem."""
    workdir = INSTALLS_DIR / install_id
    if not workdir.exists():
        raise HTTPException(404, f"install workdir not found: {install_id}")
    ij = workdir / "install.json"
    if not ij.exists():
        raise HTTPException(
            409,
            "install.json not found — either the install didn't succeed or it was cancelled",
        )
    try:
        return json.loads(ij.read_text())
    except Exception as e:
        raise HTTPException(500, f"install.json unreadable: {e}")


@router.post("/{install_id}/run", response_model=RunResponse)
async def run_install(
    install_id: str,
    body: RunRequest | None = None,
    user: User = Depends(get_current_user),
):
    """Start the installed app as a long-lived subprocess.

    - Reads install.json for the workdir, language, and run_command
    - Rebuilds the sandbox env via the language adapter
    - Spawns the command (with smoke-test artifacts stripped)
    - Blocks up to wait_for_url seconds for a URL to appear in stdout
    - Returns the handle, including the detected URL (or null if not yet found)

    Dedup: if a run for this install is already active, returns it without
    spawning a new process. Call POST /run/stop first to restart.
    """
    install_data = _load_install_json(install_id)

    if install_data.get("status") != "success":
        raise HTTPException(
            409,
            f"install is not in success state (got {install_data.get('status')})",
        )

    # 1. Determine the command
    command = None
    if body and body.command:
        command = body.command
    else:
        result = install_data.get("result") or {}
        command = result.get("run_command")
    if not command:
        raise HTTPException(
            409,
            "no run_command available — pass one explicitly in the request body",
        )

    # 2. Determine the language + rebuild sandbox env
    analysis = install_data.get("analysis") or {}
    language = analysis.get("language")
    if not language:
        raise HTTPException(
            409, "install has no language in analysis; can't rebuild sandbox"
        )
    try:
        adapter = get_adapter(language)
    except ValueError as e:
        raise HTTPException(500, f"unknown language {language}: {e}")

    workdir = INSTALLS_DIR / install_id
    try:
        sandbox_info = await asyncio.to_thread(adapter.bootstrap_sandbox, workdir)
    except Exception as e:
        raise HTTPException(500, f"sandbox rebuild failed: {e}")

    # 3. Load vault secrets so the app gets API keys injected as env vars
    secrets = vault.load()

    # 4. Spawn
    wait_for_url = body.wait_for_url if (body and body.wait_for_url is not None) else 30.0
    try:
        handle = await asyncio.to_thread(
            start_run,
            install_id=install_id,
            command=command,
            cwd=workdir,
            sandbox_env=sandbox_info.env,
            path_prepend=sandbox_info.path_prepend,
            secrets=secrets,
            wait_for_url=wait_for_url,
        )
    except Exception as e:
        log.exception("run spawn failed for %s", install_id)
        raise HTTPException(500, f"run spawn failed: {e}")

    return _serialise(handle)


@router.get("/{install_id}/run", response_model=RunResponse)
async def get_run_state(
    install_id: str,
    user: User = Depends(get_current_user),
):
    """Return the current state of the active run for this install."""
    handle = get_run_for_install(install_id)
    if not handle:
        raise HTTPException(404, "no active run for this install")
    return _serialise(handle)


@router.post("/{install_id}/run/stop", response_model=RunResponse)
async def stop_run_endpoint(
    install_id: str,
    user: User = Depends(get_current_user),
):
    """SIGTERM the running process (SIGKILL after grace period)."""
    handle = get_run_for_install(install_id)
    if not handle:
        raise HTTPException(404, "no active run for this install")
    await asyncio.to_thread(stop_run, handle.run_id)
    return _serialise(handle)


@router.get("/{install_id}/run/logs")
async def get_run_logs(
    install_id: str,
    user: User = Depends(get_current_user),
    limit: int = 200,
):
    """Return the last N lines captured from the run's stdout/stderr."""
    handle = get_run_for_install(install_id)
    if not handle:
        raise HTTPException(404, "no active run for this install")
    tail = list(handle.log_tail)
    return {
        "run_id": handle.run_id,
        "install_id": install_id,
        "status": handle.status,
        "lines": tail[-limit:],
    }
