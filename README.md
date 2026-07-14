# Knowledge Intelligence Platform

**An evidence-grounded knowledge compiler that transforms consulting assets into a connected intelligence layer of concepts, relationships, consulting patterns, and reusable expertise.**

Every AI-generated insight is traceable back to source evidence from uploaded assets. No external knowledge. No fabricated citations.

---

## Problem Statement

Organisations produce vast amounts of institutional knowledge — Points of View, Business Requirements Documents, proposal decks, architecture documents, research papers, case studies, and delivery playbooks. This knowledge is trapped across disconnected files, siloed in individual experts, and rarely reused effectively.

When a practitioner starts a new engagement, they have no reliable way to ask:
- *Has this problem been solved before, and where is the evidence?*
- *How do these concepts connect across our body of work?*
- *What is our organisation's documented position on this topic?*

The result: knowledge is repeatedly rediscovered, deliverables are drafted from scratch, and AI tools produce answers from their training data rather than from your organisation's own documented expertise.

---

## Solution

The Knowledge Intelligence Platform compiles your consulting content into a structured, connected knowledge layer:

| What you upload | What the platform builds |
|---|---|
| PDFs, DOCX, PPTX files | **Concepts** — named, typed, with verbatim source excerpts |
| Documents across a domain | **Relationships** — how concepts connect across documents |
| A body of work | **Consulting Patterns** — recurring approaches to recurring problems |
| All of the above | **Knowledge Graph** — an interactive, explorable intelligence layer |
| Questions | **Evidence-grounded answers** — every claim traced to a source document |
| A topic + audience | **Client-ready deliverables** — POVs, executive summaries, roadmaps |

---

## Key Features

### Workspace Management
Isolated knowledge domains. Each workspace contains its own documents, concepts, relationships, and patterns. No cross-workspace knowledge leakage.

### Document Processing
Upload PDF, DOCX, or PPTX files. The platform automatically runs a four-stage compilation pipeline: text extraction → concept detection → relationship mapping → pattern recognition. No manual tagging required.

### Knowledge Extraction
Three chained extraction agents compile documents into the knowledge graph:
- **Concept Agent** — extracts named entities, standards, technologies, methodologies with confidence scoring
- **Relationship Agent** — discovers how concepts connect across documents using fuzzy name resolution
- **Pattern Agent** — identifies recurring consulting patterns from the full workspace knowledge structure

### Evidence-Grounded Assistant
Ask questions in natural language. The assistant retrieves relevant context using graph-first traversal (not keyword search), assembles evidence from the knowledge graph, and generates answers that cite their exact source document and excerpt. If the answer is not in your documents, it says so explicitly. Fabrication is architecturally prevented.

### Interactive Knowledge Graph
Explore compiled knowledge as an interactive node-edge graph. Click any concept to see its definition, verbatim source evidence, connected concepts, and relationship types. Built on React Flow with strength-weighted edges.

### Deliverable Generation
Generate client-ready consulting documents (Point of View, Executive Summary, Roadmap) directly from compiled workspace knowledge. Every section cites source documents. Export as Markdown or DOCX.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  Browser  (React + TypeScript)               │
│  Workspace List │ Document Upload │ Knowledge Graph          │
│  Assistant Chat │ Deliverable Generator                      │
└───────────────────────────┬─────────────────────────────────┘
                            │  HTTP/REST — Vite proxy → :8000
                            ▼
┌─────────────────────────────────────────────────────────────┐
│            Knowledge Intelligence Platform  (FastAPI)        │
│                                                              │
│  Ingestion          Knowledge Compiler     Graph Engine      │
│  PyMuPDF            Concept Agent          NetworkX          │
│  python-docx        Relationship Agent     builder.py        │
│  python-pptx        Pattern Agent          traversal.py      │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │            SQLAlchemy ORM  (DB-agnostic)              │   │
│  │  Workspace · Document · Concept · Relationship        │   │
│  │  ConsultingPattern · Deliverable · ChatMessage        │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  Retrieval / QA              LLM (Claude via ICA)            │
│  qa_service.py               All extraction, Q&A,            │
│  deliverable_service.py      and deliverable generation      │
└─────────────────────────────────────────────────────────────┘
```

| Layer | Technology |
|---|---|
| Frontend | React 18 + TypeScript + Vite + Tailwind CSS |
| Backend | FastAPI + Python 3.9+ |
| ORM | SQLAlchemy 2.0 |
| Database | SQLite (development) → PostgreSQL (production, same ORM) |
| Graph engine | NetworkX (development) → Amazon Neptune / Neo4j (production) |
| LLM | Claude via IBM Consulting Advantage endpoint |
| Document parsing | PyMuPDF (PDF), python-docx (DOCX), python-pptx (PPTX) |

---

## Local Setup

### Prerequisites
- Python 3.9+
- Node.js 18+
- An IBM Consulting Advantage API key

### 1. Backend

```bash
cd backend

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Open .env and set CLAUDE_API_KEY
```

Start the backend:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- Database is created automatically on first startup.
- Interactive API docs: **http://localhost:8000/docs**

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**

---

## Running Tests

```bash
cd backend
source venv/bin/activate
pytest tests/ -v
```

All LLM calls are mocked. No API cost in CI.

---

## Project Structure

```
ckip/
├── backend/
│   ├── app/
│   │   ├── main.py                   # FastAPI entrypoint
│   │   ├── config.py                 # All settings via environment variables
│   │   ├── schemas.py                # Pydantic models — API I/O and grounding enforcement
│   │   ├── llm.py                    # LLM client (ICA Claude endpoint)
│   │   ├── db/
│   │   │   ├── models.py             # SQLAlchemy ORM models
│   │   │   └── session.py            # Session factory, init_db()
│   │   ├── ingestion/
│   │   │   ├── parsers.py            # PDF/DOCX/PPTX → raw text + metadata
│   │   │   └── pipeline.py           # Orchestration: parse → extract → graph
│   │   ├── extraction/
│   │   │   ├── concept_agent.py      # Concept extraction with confidence scoring
│   │   │   ├── relationship_agent.py # Relationship discovery with strength scoring
│   │   │   └── pattern_agent.py      # Consulting pattern recognition
│   │   ├── graph/
│   │   │   ├── builder.py            # NetworkX graph from SQL (derived layer)
│   │   │   └── traversal.py          # Semantic node search + strength-filtered BFS
│   │   ├── retrieval/
│   │   │   └── qa_service.py         # Evidence-grounded Q&A pipeline
│   │   ├── generation/
│   │   │   └── deliverable_service.py # POV / executive summary / roadmap generation
│   │   └── api/
│   │       ├── workspaces.py
│   │       ├── documents.py
│   │       ├── graph.py
│   │       ├── assistant.py
│   │       └── deliverables.py
│   ├── tests/
│   │   ├── test_api.py               # Integration tests — every route and error path
│   │   ├── test_graph.py             # Graph builder + traversal unit tests
│   │   ├── test_parsers.py           # PDF/DOCX/PPTX extraction tests
│   │   ├── test_pipeline.py          # Full ingestion pipeline tests
│   │   ├── test_retrieval_consistency.py # Retrieval determinism and scoring tests
│   │   ├── test_schemas.py           # Pydantic validation and grounding rule tests
│   │   └── test_stress.py            # Concurrency and large-workspace tests
│   ├── .env.example                  # Environment variable template
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── pages/
│       │   ├── WorkspaceList.tsx     # Workspace grid + create
│       │   ├── DocumentUpload.tsx    # Drag-and-drop + status polling
│       │   ├── GraphExplorer.tsx     # React Flow canvas + node side panel
│       │   ├── AssistantChat.tsx     # Chat UI with source citations
│       │   └── DeliverableGenerator.tsx # Generate + edit + export
│       ├── api/client.ts             # Typed API client (proxied to :8000)
│       └── types/api.ts              # TypeScript interfaces
└── docs/
    ├── requirements.md               # Personas, user stories, acceptance criteria
    ├── architecture.md               # System design and technical decisions
    ├── api-reference.md              # Complete API endpoint reference
    ├── demo-script.md                # Walkthrough script for demonstrations
    └── adr-payments-center-brain.md  # ADR: Payments Center Brain (future)
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_API_KEY` | — | **Required.** ICA API key |
| `CLAUDE_BASE_URL` | `https://api.nextgen-beta.ica.ibm.com/ica` | ICA Claude endpoint |
| `CLAUDE_MODEL` | `claude-sonnet-4-5` | Claude model version |
| `DATABASE_URL` | `sqlite:///./bob_knowledge_fabric.db` | SQLAlchemy DB URL |
| `UPLOAD_DIR` | `./uploads` | Local file storage path |
| `CHUNK_SIZE` | `3000` | Max characters per extraction chunk |
| `CHUNK_OVERLAP` | `200` | Character overlap between adjacent chunks |
| `GRAPH_HOP_DEPTH` | `2` | N-hop depth for graph traversal |
| `GRAPH_STRENGTH_THRESHOLD` | `0.5` | Minimum edge strength to follow in BFS |
| `GRAPH_MAX_NODES` | `60` | Max nodes expanded per traversal |
| `CONCEPT_CONFIDENCE_MIN` | `0.6` | Minimum concept confidence score to persist |
| `QA_TOP_K` | `20` | Seed nodes retrieved per Q&A query |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed CORS origins |

---

## How It Works

### 1. Knowledge Compilation (Upload → Concepts → Graph)

When you upload a document, the platform runs three chained extraction agents:

1. **Concept Agent** — reads the document text and extracts named concepts with verbatim source excerpts and confidence scores. Concepts below the confidence threshold are discarded.
2. **Relationship Agent** — looks across all workspace concepts and identifies how this document's concepts connect to existing ones. Relationship strength is scored and stored.
3. **Pattern Agent** — examines the full workspace concept and relationship map and identifies recurring consulting patterns.

Every extracted item carries a `source_document_id` and `source_excerpt`. Items without traceable sources are rejected before storage.

### 2. Evidence-Grounded Q&A (Question → Answer)

When you ask a question:

1. Keywords are extracted from the question (LLM-assisted, temperature=0)
2. Concept nodes are scored using **cumulative multi-signal matching** across all keywords — exact name, substring, type, description — weighted by concept confidence
3. The top seeds are expanded via **strength-filtered BFS** through the knowledge graph
4. Retrieved concepts are sorted deterministically: seed relevance DESC, confidence DESC, then concept ID as tie-break
5. Near-duplicate source excerpts are deduplicated before the context window is assembled
6. Claude generates an answer from the context at temperature=0, citing every claim with `[Source: Document Name]`
7. If the answer is non-empty but sources are empty, it is rejected — the grounding rule is enforced at the `SourcedResponseMixin` schema level, not just in a prompt

### 3. Grounding Rule (Hard Constraint)

The platform does not use internet search, web browsing, or general knowledge for any compilation, answer, or deliverable. The `SourcedResponseMixin` in `schemas.py` is a Pydantic `model_validator` that runs before any AI response leaves the API. An answer without sources is a schema validation error.

---

## Production Upgrade Path

Every component is designed for swap-in replacement without code changes:

| Component | Development | Production |
|---|---|---|
| Database | SQLite | RDS PostgreSQL (change `DATABASE_URL`) |
| Graph engine | NetworkX in-process | Amazon Neptune / Neo4j |
| Task queue | FastAPI BackgroundTasks | SQS + Celery worker fleet |
| File storage | Local `./uploads` | S3 |
| Deployment | `uvicorn` local | ECS Fargate + CloudFront |
| Authentication | None | IBM SSO / Cognito |
| LLM endpoint | ICA Claude direct | Amazon Bedrock (Claude) |

---

## Roadmap

See [`docs/adr-payments-center-brain.md`](docs/adr-payments-center-brain.md) for the next major capability: a shared institutional knowledge graph that practitioners can optionally include when generating deliverables.

Out of scope for current version: authentication, multi-tenant isolation, CI/CD pipeline, production deployment automation, real-time collaborative editing, enterprise system integrations (SharePoint, Box, Teams, Confluence), document versioning.
