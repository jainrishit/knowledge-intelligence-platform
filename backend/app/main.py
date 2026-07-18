"""
Knowledge Intelligence Platform — FastAPI application entrypoint.

Middleware stack (outermost → innermost):
  RequestLoggingMiddleware — structured per-request logging with request_id,
                             endpoint, elapsed time, and status code
  CORSMiddleware           — cross-origin resource sharing

Global exception handlers:
  HTTPException            — passthrough (already an HTTP response)
  Exception (catch-all)    — logs full traceback; returns generic 500 JSON
                             so no raw tracebacks ever reach the client
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.db.session import init_db
from app.api import workspaces, documents, graph, assistant, deliverables, admin, plans, audit, certification

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Request logging middleware
# ─────────────────────────────────────────────────────────────────────────────

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Adds a unique request_id to every request and logs:
      - method, path, workspace_id (extracted from path when present)
      - response status code
      - elapsed time in milliseconds

    All logging happens in the backend only — nothing is exposed to clients.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:8]
        t0 = time.monotonic()

        # Best-effort workspace_id extraction from path (/workspaces/{id}/...)
        workspace_id = _extract_workspace_id(request.url.path)
        ws_tag = f" ws={workspace_id}" if workspace_id else ""

        logger.info(
            "[req=%s%s] → %s %s",
            request_id, ws_tag, request.method, request.url.path,
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            logger.error(
                "[req=%s%s] ✗ unhandled %s: %s (%.0fms)",
                request_id, ws_tag, type(exc).__name__, exc, elapsed,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "An unexpected server error occurred. Please try again."},
            )

        elapsed = (time.monotonic() - t0) * 1000
        log_fn = logger.warning if response.status_code >= 400 else logger.info
        log_fn(
            "[req=%s%s] ← %d %.0fms",
            request_id, ws_tag, response.status_code, elapsed,
        )
        return response


def _extract_workspace_id(path: str) -> Optional[str]:
    """Extract workspace_id from URL paths like /workspaces/42/... or /workspace/42/..."""
    parts = path.strip("/").split("/")
    for i, part in enumerate(parts):
        if part in ("workspaces", "workspace") and i + 1 < len(parts):
            candidate = parts[i + 1]
            if candidate.isdigit():
                return candidate
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Application lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager.startup_load()
    yield


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Knowledge Intelligence Platform",
    description=(
        "An evidence-grounded knowledge compiler that transforms consulting assets into a connected "
        "intelligence layer of concepts, relationships, consulting patterns, and reusable expertise. "
        "Every AI-generated insight is traceable back to source evidence from uploaded assets — "
        "no external knowledge, no fabricated citations."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ── Global exception handler — catch any unhandled exception before it ────────
# becomes a raw 500 with a Python traceback in the response body.

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch-all for any exception not handled by a specific endpoint.

    Logs the full traceback to the backend log (never sent to client).
    Returns a clean JSON 500 with a generic user message.
    """
    workspace_id = _extract_workspace_id(request.url.path)
    ws_tag = f" ws={workspace_id}" if workspace_id else ""
    logger.error(
        "[global_handler%s] Unhandled %s on %s %s",
        ws_tag, type(exc).__name__, request.method, request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred. Please try again."},
    )


# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(workspaces.router)
app.include_router(documents.router)
app.include_router(graph.router)
app.include_router(assistant.router)
app.include_router(deliverables.router)
app.include_router(plans.router)
app.include_router(audit.router)
app.include_router(certification.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "Knowledge Intelligence Platform"}
