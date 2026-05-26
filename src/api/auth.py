"""Centralized auth dependencies for the FastAPI app."""

import secrets

from fastapi import Header, HTTPException, Request


def require_admin_token(
    request: Request, authorization: str | None = Header(None)
) -> None:
    """Reject the request unless ``Authorization: Bearer <admin token>`` matches.

    Returns 503 if the server has no admin token configured — explicit
    refusal beats accidental open admin. 401 for missing/malformed
    header, 403 for the wrong value.
    """
    expected = request.app.state.settings.admin.token
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API disabled")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    provided = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid admin token")
