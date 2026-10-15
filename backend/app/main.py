"""BriefDesk: one FastAPI process serves the API, the WebSocket chat and the HTML/CSS/JS frontend.

    cd backend && uvicorn app.main:app      ->  http://127.0.0.1:8000
"""
import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from .api.routes import account, admin, auth, briefs, ws
from .bootstrap import ensure_admin, init_db, warn_insecure_defaults
from .core.config import BACKEND_DIR, FRONTEND_DIR, get_settings
from .core.logging import request_id_var, setup_logging
from .core.ratelimit import limiter
from .db.session import SessionLocal
from .llm.gateway import llm_status
from .services.hub import hub
from .services.runner import get_runner, shutdown_runner
from .tools.mcp_client import shutdown_hub, start_hub_background

settings = get_settings()
setup_logging(settings.log_level, settings.log_format, settings.log_file, base_dir=BACKEND_DIR)
log = logging.getLogger("app")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; font-src 'self'; "
        "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": _CSP,
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_admin(db)
        warn_insecure_defaults(db)
    hub.bind(asyncio.get_running_loop())
    if settings.mcp_client_enabled:
        start_hub_background()
    get_runner().recover()
    llm = llm_status()
    log.info("startup complete", extra={"env": settings.environment, "llm": llm["mode"], "llm_ready": llm["ready"],
                                        "db": settings.database_url.split(":")[0]})
    if not llm["ready"]:
        log.warning("LLM not ready: %s", llm.get("problem"))
    yield
    shutdown_runner()
    shutdown_hub()


app = FastAPI(
    title=f"{settings.app_name} API",
    version="1.0.0",
    description="Multi-agent research briefs: Supervisor + Researcher (ReAct, web + MCP tools) + Writer + Reviewer "
                "+ Citation Verifier + Publisher, with a live two-way chat.",
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# No CORS middleware on purpose: the frontend is same-origin; other origins get no CORS grants.


@app.middleware("http")
async def request_context(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID", "")
    rid = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error")
        response = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
    response.headers["X-Request-ID"] = rid
    path = request.url.path
    for name, value in _SECURITY_HEADERS.items():
        if path.startswith("/docs") and name == "Content-Security-Policy":
            continue                                    # Swagger UI loads its own scripts from a CDN
        response.headers.setdefault(name, value)
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif not path.startswith("/docs"):
        response.headers.setdefault("Cache-Control", "no-cache")   # static files: always revalidate (ETag)
    if path.startswith("/api/") or response.status_code >= 400:
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        log.log(level, "request", extra={"method": request.method, "path": path, "status": response.status_code,
                                         "duration_ms": round((time.perf_counter() - start) * 1000, 1)})
    return response


API = "/api/v1"
for r in (auth.router, briefs.router, account.router, admin.router, ws.router):
    app.include_router(r, prefix=API)


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready():
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    llm = llm_status()
    return {"status": "ready", "llm": llm["mode"], "llm_ready": llm["ready"],
            "breakers": llm.get("breakers", {})}


if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
