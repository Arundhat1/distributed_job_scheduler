"""
Authentication for worker-service endpoints (/api/v1/workers/*), which
are called by scripts/run_worker.py — a non-human, non-JWT client.

This is intentionally a separate, parallel auth scheme from
app.deps.get_current_user (JWT + User row lookup). Workers authenticate
with a single shared secret (settings.scheduler_token), sent the same
way a JWT would be (`Authorization: Bearer <token>`) so scripts/run_worker.py
needs no protocol change, but verified with a plain constant-time string
comparison instead of JWT decode + DB lookup. See docs/DESIGN_DECISIONS.md
for the trade-offs (one shared secret vs. per-worker credentials).

Fails closed: if scheduler_token is unset/empty on the server, no bearer
value can ever match it (hmac.compare_digest against an empty configured
secret still requires an equally-empty token AND we explicitly reject
that case below), so a misconfigured deployment rejects all worker
traffic rather than accepting anything.
"""
import hmac

from fastapi import HTTPException, Request, status

from app.config import get_settings

settings = get_settings()


async def verify_worker_token(request: Request) -> None:
    configured = settings.scheduler_token

    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    # Never log `token` or `configured` anywhere below this line.
    if not configured or not token or not hmac.compare_digest(token, configured):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing worker token",
        )