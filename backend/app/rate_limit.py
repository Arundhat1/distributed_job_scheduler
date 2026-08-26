"""
Shared rate limiter instance, in its own module so both app.main (which
wires up the middleware/exception handler) and individual routers (which
apply endpoint-specific @limiter.limit(...) decorators) can import the
same object without a circular import.

Consistency model — be explicit about what this does and doesn't cover:
  - Storage is slowapi's default in-memory backend: correct and sufficient
    for the single-API-instance deployment this project ships
    (docker-compose.yml runs exactly one `api` container). It is NOT
    correct across multiple API instances — each instance keeps its own
    independent counters, so the effective limit becomes
    (configured limit) x (instance count) rather than a shared budget.
    The fix, if the API ever scales horizontally, is to pass
    storage_uri="redis://..." to Limiter — the key function and every
    @limiter.limit(...) call site stay unchanged.

Key function — fairness, not just abuse prevention:
  - Requests carrying a valid bearer token are keyed by the authenticated
    user id ("user:<id>"), not by IP. Multiple worker processes or
    multiple users behind the same NAT/load balancer would otherwise
    share one IP-based budget and throttle each other's legitimate
    traffic — this project's own docker-compose.yml runs two worker
    containers that would hit exactly that problem under a naive
    IP-keyed limiter.
  - Requests without a valid token (login, register — there's no subject
    to key by yet) fall back to remote IP, which is the right and only
    option pre-authentication and is what protects those endpoints from
    credential-stuffing / signup-spam.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from .security import decode_access_token


def rate_limit_key(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
        payload = decode_access_token(token)
        if payload and "sub" in payload:
            return f"user:{payload['sub']}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=rate_limit_key, default_limits=["300/minute"])