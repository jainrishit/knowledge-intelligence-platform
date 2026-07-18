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

## LLM Governance

Every LLM call passes through a governance pipeline before reaching the provider:

| Layer | What it does |
|---|---|
| **Secret Provider** | API key fetched via `SecretProvider` abstraction — never directly from `os.environ` in business logic. Swap to `AWSSecretsManagerProvider` for production with zero code changes |
| **Circuit Breaker** | Opens after 5 consecutive failures; auto-recovers after 60s. Rejects all calls while open (HTTP 503) to prevent cascade failures |
| **Rate Limiter** | Per-workspace sliding-window counter — default 60 requests/min. Returns HTTP 429 when exceeded |
| **Budget Check** | Per-workspace monthly token and cost limits enforced before each call. Returns HTTP 429 when exceeded |
| **Usage Tracker** | Every call writes one `LLMUsage` row with token counts and estimated cost. Running totals accumulated in `LLMBudget` |
| **Observability** | Structured log lines include workspace ID, operation, latency, token counts, and cost estimate. API keys, prompts, and credentials are never logged |

Admin visibility: `GET /admin/llm/usage` and `GET /admin/llm/budgets` return usage aggregates and budget status.

---

## Upload Security

Every uploaded file passes through a three-stage validation pipeline before it touches the filesystem or a parser:

| Stage | What is checked | How |
|---|---|---|
| **Magic-byte detection** | Actual file format — not the filename or `Content-Type` header | `filetype` library reads the binary signature from the first 8 KB |
| **Allow-list enforcement** | Only PDF, DOCX, PPTX are accepted | Any other detected MIME type returns HTTP 422 |
| **Size limit** | Default 25 MB per file, configurable via `MAX_UPLOAD_BYTES` | Streamed check — oversized files return HTTP 413 before being fully buffered |

**Why magic bytes instead of extensions?** A file named `malware.pdf` with a ZIP or executable binary inside will pass an extension check but fail the magic-byte check. Attackers control the filename and `Content-Type` header; they do not control the binary content signature.

All parsers (PyMuPDF, python-docx, python-pptx) are wrapped with defensive exception handling. A corrupt or malformed file marks the document as `failed` with a clean error message — no stack traces are ever exposed to the client.

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
│                 └── Plan → Review → Approve → Generate PPTX │
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
│  │  PresentationPlan                                     │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  Retrieval / QA              LLM (Claude via ICA)            │
│  qa_service.py               All extraction, Q&A,            │
│  plan_service.py             blueprint generation, and       │
│  deliverable_service.py      PPTX rendering                  │
└─────────────────────────────────────────────────────────────┘
```

### Presentation Workflow

```
User selects type     Claude analyses graph    Claude returns plan
(Client 101/201 or  →  (concepts, relations,  →  (slide-by-slide with
 Executive Summary)     patterns, evidence)       key insights, grounding)
                                                          ↓
PPTX downloaded  ←  PowerPoint render  ←  Approve  ←  User reviews
(IBM Asset Kit)     (no LLM calls)       & generate   revises, reorders
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
│   │   ├── llm_client.py             # LLM client (ICA Claude endpoint)
│   │   ├── db/
│   │   │   ├── models.py             # SQLAlchemy ORM models (incl. PresentationPlan)
│   │   │   └── session.py            # Session factory, init_db()
│   │   ├── core/
│   │   │   └── upload_validation.py  # Magic-byte detection, size limit, MIME allow-list
│   │   ├── security/
│   │   │   └── secrets.py            # SecretProvider abstraction layer
│   │   ├── llm/
│   │   │   ├── circuit_breaker.py    # CLOSED/OPEN/HALF_OPEN circuit breaker
│   │   │   ├── governance.py         # Budget enforcement + rate limiting
│   │   │   └── usage_tracker.py      # Token/cost recording per API call
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
│   │   │   ├── plan_service.py       # Plan workflow: generate → revise → approve
│   │   │   └── deliverable_service.py # Blueprint, validation, PPTX normaliser
│   │   └── api/
│   │       ├── workspaces.py
│   │       ├── documents.py
│   │       ├── graph.py
│   │       ├── assistant.py
│   │       ├── plans.py              # Plan workflow API (6 routes)
│   │       ├── deliverables.py
│   │       └── admin.py              # LLM usage + budget visibility endpoints
│   ├── tests/
│   │   ├── test_api.py               # Integration tests — every route and error path
│   │   ├── test_graph.py             # Graph builder + traversal unit tests
│   │   ├── test_llm_governance.py    # Secrets, budget, circuit breaker, rate limiter
│   │   ├── test_llm_resilience.py    # Timeout, retry, SDK double-retry prevention
│   │   ├── test_parsers.py           # PDF/DOCX/PPTX extraction tests
│   │   ├── test_pipeline.py          # Full ingestion pipeline tests
│   │   ├── test_plan_workflow.py     # Plan generate → revise → approve workflow tests
│   │   ├── test_retrieval_consistency.py # Retrieval determinism and scoring tests
│   │   ├── test_schemas.py           # Pydantic validation and grounding rule tests
│   │   ├── test_stability.py         # Stability and regression tests
│   │   ├── test_stress.py            # Concurrency and large-workspace tests
│   │   └── test_upload_security.py   # File size, magic-byte, and spoofing tests
│   ├── .env.example                  # Environment variable template
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── pages/
│       │   ├── WorkspaceList.tsx     # Workspace grid + create
│       │   ├── DocumentUpload.tsx    # Drag-and-drop + status polling
│       │   ├── GraphExplorer.tsx     # React Flow canvas + node side panel
│       │   ├── AssistantChat.tsx     # Chat UI with source citations
│       │   └── DeliverableGenerator.tsx # Plan → review → approve → generate PPTX
│       ├── api/client.ts             # Typed API client (proxied to :8000)
│       └── types/api.ts              # TypeScript interfaces
└── docs/
    ├── adr-payments-center-brain.md  # ADR: Payments Center Brain (future)
```

---

## Environment Variables

| Variable                               | Default                                    | Description                                                                    |
| ----------------------------------------| --------------------------------------------| --------------------------------------------------------------------------------|
| `CLAUDE_API_KEY`                       | —                                          | Raw API key. Local dev only — leave blank in production                        |
| `CLAUDE_SECRET_NAME`                   | `CLAUDE_API_KEY`                           | Secret name resolved by `SecretProvider` at runtime                            |
| `CLAUDE_BASE_URL`                      | `https://api.nextgen-beta.ica.ibm.com/ica` | ICA Claude endpoint                                                            |
| `CLAUDE_MODEL`                         | `claude-sonnet-4-5`                        | Claude model version                                                           |
| `DATABASE_URL`                         | `sqlite:///./knowledge_platform.db`        | SQLAlchemy DB URL                                                              |
| `UPLOAD_DIR`                           | `./uploads`                                | Local file storage path                                                        |
| `MAX_UPLOAD_BYTES`                     | `26214400`                                 | Maximum upload size per file (default 25 MB)                                   |
| `CHUNK_SIZE`                           | `3000`                                     | Max characters per extraction chunk                                            |
| `CHUNK_OVERLAP`                        | `200`                                      | Character overlap between adjacent chunks                                      |
| `GRAPH_HOP_DEPTH`                      | `2`                                        | N-hop depth for graph traversal                                                |
| `GRAPH_STRENGTH_THRESHOLD`             | `0.35`                                     | Minimum edge strength to follow in BFS                                         |
| `GRAPH_MAX_NODES`                      | `100`                                      | Max nodes expanded per traversal                                               |
| `CONCEPT_CONFIDENCE_MIN`               | `0.6`                                      | Minimum concept confidence score to persist                                    |
| `PATTERN_EXTRACTION_THRESHOLD_PERCENT` | `10`                                       | Minimum % growth in concepts or relationships to trigger pattern re-extraction |
| `QA_TOP_K`                             | `30`                                       | Seed nodes retrieved per Q&A query                                             |
| `LLM_CB_FAILURE_THRESHOLD`             | `5`                                        | Consecutive failures before circuit breaker opens                              |
| `LLM_CB_RECOVERY_TIMEOUT`             | `60`                                       | Seconds before breaker transitions to half-open                                |
| `MAX_LLM_REQUESTS_PER_MINUTE`          | `60`                                       | Per-workspace LLM request rate limit                                           |
| `LLM_RATE_WINDOW_SECONDS`              | `60`                                       | Sliding window duration for rate limiting                                      |
| `LLM_COST_PER_1K_INPUT_TOKENS`         | `0.003`                                    | USD cost per 1 000 input tokens                                                |
| `LLM_COST_PER_1K_OUTPUT_TOKENS`        | `0.015`                                    | USD cost per 1 000 output tokens                                               |
| `LLM_CONNECT_TIMEOUT`                  | `10`                                       | Seconds to wait for TCP connection + TLS handshake                             |
| `LLM_READ_TIMEOUT`                     | `180`                                      | Seconds to wait for the first response byte from the LLM                       |
| `LLM_WRITE_TIMEOUT`                    | `30`                                       | Seconds to wait while uploading the request body                               |
| `LLM_MAX_RETRIES`                      | `1`                                        | Retry attempts after the initial call (2 total attempts)                       |
| `LLM_RETRY_MAX_WAIT`                   | `8`                                        | Backoff ceiling in seconds (exponential + jitter)                              |
| `CORS_ORIGINS`                         | `http://localhost:5173`                    | Comma-separated allowed CORS origins                                           |

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

| Component      | Development             | Production                             |
| ----------------| -------------------------| ----------------------------------------|
| Database       | SQLite                  | RDS PostgreSQL (change `DATABASE_URL`) |
| Graph engine   | NetworkX in-process     | Amazon Neptune / Neo4j                 |
| Task queue     | FastAPI BackgroundTasks | SQS + Celery worker fleet              |
| File storage   | Local `./uploads`       | S3                                     |
| Deployment     | `uvicorn` local         | ECS Fargate + CloudFront               |
| Authentication | None                    | IBM SSO / Cognito                      |
| LLM endpoint   | ICA Claude direct       | Amazon Bedrock (Claude)                |

---

## Roadmap

See [`docs/adr-payments-center-brain.md`](docs/adr-payments-center-brain.md) for the next major capability: a shared institutional knowledge graph that practitioners can optionally include when generating deliverables.

Out of scope for current version: authentication, multi-tenant isolation, CI/CD pipeline, production deployment automation, real-time collaborative editing, enterprise system integrations (SharePoint, Box, Teams, Confluence), document versioning.
