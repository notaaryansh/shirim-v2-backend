"""LLM proxy — forwards chat-completion requests to OpenAI.

The Electron frontend runs the agent loop locally but calls this endpoint
for each LLM turn so the OpenAI API key never leaves the server.
"""
import logging
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..config import OPENAI_CLIENT

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agent", tags=["agent"])

# ---------------------------------------------------------------------------
# Rate limiting (in-memory, per user)
# ---------------------------------------------------------------------------
_RATE_LIMIT_PER_MINUTE = 100
_RATE_LIMIT_PER_DAY = 2000

_minute_counts: dict[str, list[float]] = defaultdict(list)
_day_counts: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(user_id: str) -> None:
    now = time.time()

    # Per-minute check
    bucket = _minute_counts[user_id]
    bucket[:] = [t for t in bucket if now - t < 60]
    if len(bucket) >= _RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded (per minute)",
        )

    # Per-day check
    bucket_day = _day_counts[user_id]
    bucket_day[:] = [t for t in bucket_day if now - t < 86400]
    if len(bucket_day) >= _RATE_LIMIT_PER_DAY:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded (per day)",
        )

    bucket.append(now)
    bucket_day.append(now)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class AgentCompletionRequest(BaseModel):
    messages: list[dict]
    tool_schemas: list[dict]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.post("/completion")
async def agent_completion(
    req: AgentCompletionRequest,
    user: User = Depends(get_current_user),
):
    """Proxy a single chat-completion turn to OpenAI.

    The client sends messages + tool schemas; the server forwards them
    using its own API key and returns the raw OpenAI response.  The model
    is server-controlled (always gpt-5.4-mini) to prevent abuse.
    """
    if OPENAI_CLIENT is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OpenAI API key not configured",
        )

    _check_rate_limit(user.id)

    try:
        resp = OPENAI_CLIENT.chat.completions.create(
            model="gpt-5.4-mini",
            messages=req.messages,
            tools=req.tool_schemas if req.tool_schemas else None,
            temperature=0.2,
        )
        return resp.model_dump()
    except Exception as e:
        log.error("[agent] OpenAI call failed: %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM call failed: {type(e).__name__}",
        )
