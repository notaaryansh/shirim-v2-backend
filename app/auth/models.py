from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    """User model returned by auth validation."""
    id: str
    email: str
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    access_token: Optional[str] = None
