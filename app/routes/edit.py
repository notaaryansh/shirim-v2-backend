"""/api/v1/install/{install_id}/edit — Edit with AI endpoints."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..agent.editor import (
    EditSession,
    create_session,
    get_session,
    get_session_for_install,
    run_edit_turn,
    undo_turn,
)
from ..agent.sandbox import INSTALLS_DIR
from ..auth.dependencies import get_current_user
from ..auth.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/install", tags=["edit"])


class EditMessageRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class UndoRequest(BaseModel):
    turn_id: int


def _serialize_session(s: EditSession) -> dict:
    return {
        "session_id": s.session_id,
        "install_id": s.install_id,
        "app_context": {
            "project_type": s.app_context.get("project_type"),
            "framework": s.app_context.get("framework"),
            "styling": s.app_context.get("styling"),
            "components_count": len(s.app_context.get("components", [])),
        },
        "turn_count": len(s.turns),
        "turns": [
            {
                "turn_id": t.turn_id,
                "user_message": t.user_message,
                "status": t.status,
                "files_changed": t.files_changed,
                "tsc_ok": t.tsc_ok,
                "agent_reply": t.agent_reply,
                "started_at": t.started_at,
                "finished_at": t.finished_at,
            }
            for t in s.turns
        ],
    }


@router.post("/{install_id}/edit")
async def send_edit_message(
    install_id: str,
    body: EditMessageRequest,
    user: User = Depends(get_current_user),
):
    """Send a natural-language edit request.

    Creates a session on first call (or reuses an existing one).
    Runs the edit agent synchronously — returns when the edit is complete.
    The app's dev server will hot-reload changed files automatically.
    """
    workdir = INSTALLS_DIR / install_id
    if not workdir.exists():
        raise HTTPException(404, "install not found")

    # Get or create session.
    session = None
    if body.session_id:
        session = get_session(body.session_id)
    if session is None:
        session = get_session_for_install(install_id)
    if session is None:
        sid = body.session_id or uuid.uuid4().hex[:12]
        try:
            session = create_session(install_id, sid)
        except FileNotFoundError:
            raise HTTPException(404, "install workdir not found")

    # Run the edit turn.
    try:
        turn = await run_edit_turn(session, body.message)
    except Exception as e:
        log.exception("edit turn failed")
        raise HTTPException(500, f"edit failed: {e}")

    return {
        "session_id": session.session_id,
        "turn": {
            "turn_id": turn.turn_id,
            "status": turn.status,
            "files_changed": turn.files_changed,
            "tsc_ok": turn.tsc_ok,
            "tsc_errors": turn.tsc_errors,
            "agent_reply": turn.agent_reply,
            "duration_ms": int((turn.finished_at - turn.started_at) * 1000)
            if turn.finished_at
            else 0,
        },
    }


@router.get("/{install_id}/edit")
async def get_edit_session(
    install_id: str,
    user: User = Depends(get_current_user),
):
    """Get the current edit session for an install (conversation history)."""
    session = get_session_for_install(install_id)
    if not session:
        raise HTTPException(404, "no edit session for this install")
    return _serialize_session(session)


@router.post("/{install_id}/edit/undo")
async def undo_edit(
    install_id: str,
    body: UndoRequest,
    user: User = Depends(get_current_user),
):
    """Undo a specific edit turn by restoring from its snapshot."""
    session = get_session_for_install(install_id)
    if not session:
        raise HTTPException(404, "no edit session for this install")

    if body.turn_id < 0 or body.turn_id >= len(session.turns):
        raise HTTPException(400, f"invalid turn_id: {body.turn_id}")

    ok = undo_turn(session, body.turn_id)
    if not ok:
        raise HTTPException(404, f"no snapshot found for turn {body.turn_id}")

    return {"ok": True, "restored_to_before_turn": body.turn_id}
