"""Private-preview identity boundary. Bangumi provider tokens are unrelated."""

import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import get_settings

_bearer = HTTPBearer(auto_error=False)
_bearer_credentials = Depends(_bearer)


def validate_auth_configuration() -> None:
    settings = get_settings()
    if not settings.AUTH_ENABLED:
        if settings.ENVIRONMENT == "production":
            raise RuntimeError("Production requires AUTH_ENABLED=true")
        return
    tokens = settings.API_USER_TOKENS
    if not tokens or any(
        not user.strip() or user != user.strip() or user == "anonymous"
        or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token)
        for user, token in tokens.items()
    ) or len(set(tokens.values())) != len(tokens):
        raise RuntimeError("API_USER_TOKENS requires named users and distinct ASCII tokens of at least 32 characters")


async def authenticated_user(
    credentials: HTTPAuthorizationCredentials | None = _bearer_credentials,
) -> str:
    settings = get_settings()
    try:
        validate_auth_configuration()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Authentication is not configured") from None
    if not settings.AUTH_ENABLED:
        return "anonymous"
    if credentials is not None:
        for user, token in settings.API_USER_TOKENS.items():
            if secrets.compare_digest(credentials.credentials.encode(), token.encode()):
                return user
    raise HTTPException(status_code=401, detail="Invalid or missing bearer token",
                        headers={"WWW-Authenticate": "Bearer"})
