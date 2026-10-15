"""Rate limiting (slowapi, in-memory; use storage_uri="redis://..." for several instances).

Authenticated requests are limited per USER (JWT subject or API token), anonymous ones per client IP.
X-Forwarded-For is honoured only when the TCP peer is a trusted reverse proxy (TRUSTED_PROXIES),
so a client cannot spoof its way into someone else's bucket.
"""
import hashlib

import jwt
from fastapi import Request
from slowapi import Limiter

from .config import get_settings
from .security import decode_access_token


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in get_settings().trusted_proxies:
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if first and len(first) <= 64:
            return first
    return peer


def user_or_ip(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token.startswith("bd_"):                 # personal API token (MCP server, scripts)
            return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:16]
        try:
            return f"user:{decode_access_token(token)['sub']}"
        except jwt.PyJWTError:
            pass                                   # invalid token -> treat as anonymous
    return f"ip:{client_ip(request)}"


limiter = Limiter(key_func=user_or_ip)
