"""
Knowledge Intelligence Platform — FastAPI application entrypoint.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.session import init_db
from app.api import workspaces, documents, graph, assistant, deliverables, admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager.startup_load()
    yield


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workspaces.router)
app.include_router(documents.router)
app.include_router(graph.router)
app.include_router(assistant.router)
app.include_router(deliverables.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "Knowledge Intelligence Platform"}
