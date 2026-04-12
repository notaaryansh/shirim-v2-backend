"""/api/v1/secrets — CRUD for the user's API key vault."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..auth.models import User
from .. import vault

router = APIRouter(prefix="/v1/secrets", tags=["secrets"])


class SecretInput(BaseModel):
    name: str
    value: str


class CheckRequest(BaseModel):
    names: list[str]


@router.get("")
async def list_secrets(user: User = Depends(get_current_user)):
    """List all stored secrets with masked values. Safe to display in UI."""
    return {"secrets": vault.list_masked()}


@router.post("")
async def add_secret(body: SecretInput, user: User = Depends(get_current_user)):
    """Add or update a secret."""
    name = body.name.strip().upper()
    if not name:
        raise HTTPException(400, "name is required")
    if not body.value.strip():
        raise HTTPException(400, "value is required")
    vault.set_key(name, body.value.strip())
    return {"ok": True, "name": name, "masked_value": vault.mask(body.value.strip())}


@router.delete("/{name}")
async def delete_secret(name: str, user: User = Depends(get_current_user)):
    """Remove a secret."""
    if not vault.delete_key(name):
        raise HTTPException(404, f"secret '{name}' not found")
    return {"ok": True, "name": name}


@router.get("/{name}/reveal")
async def reveal_secret(name: str, user: User = Depends(get_current_user)):
    """Return the unmasked value. Use sparingly — for copy/edit in the UI."""
    value = vault.get(name)
    if value is None:
        raise HTTPException(404, f"secret '{name}' not found")
    return {"name": name, "value": value}


@router.post("/check")
async def check_secrets(body: CheckRequest, user: User = Depends(get_current_user)):
    """Given a list of env var names, report which ones are in the vault.

    Designed to be called after install analysis to show the user which
    required keys are missing before they click Run.
    """
    return {"status": vault.check(body.names)}
