"""Email OTP auth endpoints. Ported from shirim/api/auth_routes.py.

All auth happens on the backend — the frontend never talks to Supabase directly.
"""
import logging
import traceback

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

logger = logging.getLogger(__name__)

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..supabase_client import get_client

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class OTPRequest(BaseModel):
    email: EmailStr


class VerifyOTPRequest(BaseModel):
    email: EmailStr
    otp: str


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/send-otp")
async def send_otp(request: OTPRequest):
    """Send a 6-digit OTP to the user's email.

    Frontend calls this with the user's email. The user receives a 6-digit
    code via email. Frontend then calls /verify-otp with email + otp.
    """
    try:
        client = get_client()
        client.auth.sign_in_with_otp(
            {
                "email": request.email,
                "options": {"should_create_user": True},
            }
        )
        return {
            "success": True,
            "message": "Check your email for the 6-digit code",
        }
    except Exception as e:
        error_msg = str(e)
        status = getattr(e, "status", None) or getattr(e, "code", None)
        details = {
            "type": type(e).__name__,
            "message": error_msg,
            "status": status,
            "args": [repr(a) for a in getattr(e, "args", [])],
            "attrs": {
                k: repr(getattr(e, k, None))
                for k in ("message", "code", "status", "error", "details", "hint", "json", "response")
                if hasattr(e, k)
            },
        }
        logger.error("send_otp failed: %s", details)
        logger.error("traceback:\n%s", traceback.format_exc())

        if "429" in error_msg or "rate" in error_msg.lower() or status == 429:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please wait before trying again.",
            )
        raise HTTPException(
            status_code=502,
            detail=f"Supabase auth error [{details['type']}] status={status}: {error_msg}",
        )


@router.post("/verify-otp")
async def verify_otp(request: VerifyOTPRequest):
    """Verify OTP and return session tokens."""
    try:
        client = get_client()
        response = client.auth.verify_otp(
            {
                "email": request.email,
                "token": request.otp,
                "type": "email",
            }
        )
        session = response.session
        user = response.user

        if not session:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")

        name = None
        if user and user.user_metadata:
            name = user.user_metadata.get("full_name") or user.user_metadata.get(
                "name"
            )

        return {
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expires_in": session.expires_in,
            "user": {
                "id": user.id,
                "email": user.email,
                "name": name,
            },
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")


@router.post("/refresh")
async def refresh_session(request: RefreshRequest):
    """Exchange a refresh token for a fresh access/refresh pair."""
    try:
        client = get_client()
        response = client.auth.refresh_session(request.refresh_token)
        session = response.session
        if not session:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        return {
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expires_in": session.expires_in,
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


@router.post("/sign-out")
async def sign_out(user: User = Depends(get_current_user)):
    """Invalidate the current session on Supabase."""
    try:
        client = get_client()
        client.auth.sign_out()
        return {"success": True, "message": "Signed out"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    """Return the current authenticated user."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
    }
