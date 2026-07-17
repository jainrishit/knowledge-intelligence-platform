# Knowledge Intelligence Platform — System Architecture

---

## 1. Vision & Positioning

The Knowledge Intelligence Platform is an **evidence-grounded knowledge compiler** for IBM Consulting.
It is not a chatbot, not a document search tool, and not a generic RAG pipeline.
It transforms fragmented institutional knowledge — POVs, BRDs, case studies, playbooks, architecture documents — into a structured, connected, evidence-traceable knowledge layer.

**The grounding rule is architectural, not just a prompt constraint:**
Every compiled item (concept, relationship, pattern) carries a `source_document_id` and `source_excerpt` in the database.
Every AI-generated response (answer, comparison, deliverable) returns a structured `sources` field — enforced at the Pydantic schema level before any response leaves the API.

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    BROWSER  (React + TypeScript)                  │
│                                                                  │
│  Workspace List  │  Doc Upload  │  Graph Explorer                │
│  Assistant Chat  │  Deliverable Generator                        │
└────────────────────────┬─────────────────────────────────────────┘
                         │  HTTP/REST (JSON) — Vite proxy → :8000
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│          KNOWLEDGE INTELLIGENCE PLATFORM  —  FastAPI (Python)     │
│                                                                  │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────┐  │
│  │  Document        │  │  Knowledge       │  │  Graph Engine  │  │
│  │  Ingestion       │  │  Compiler        │  │  NetworkX      │  │
│  │  PyMuPDF         │  │  Concept Agent   │  │  builder.py    │  │
│  │  python-docx     │  │  Relationship    │  │  traversal.py  │  │
│  │  python-pptx     │  │  Agent           │  └───────┬────────┘  │
│  └──────┬───────────┘  │  Pattern Agent   │          │           │
│         │              └──────┬───────────┘          │           │
│         │                     │                      │           │
│  ┌──────▼─────────────────────▼──────────────────────▼───────┐  │
│  │                 SQLAlchemy ORM (DB-agnostic)               │  │
│  │  Workspace · Document · Concept · Relationship · Pattern   │  │
│  │  Deliverable · ChatMessage                                 │  │
│  └──────────────────────────────┬────────────────────────────┘  │
│                                 │                                │
│  ┌──────────────────────────────▼────────────────────────────┐  │
│  │       SQLite (MVP)  ·  PostgreSQL (Phase 2, same ORM)      │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Retrieval / QA          Anthropic Claude API              │  │
│  │  qa_service.py           (all extraction, Q&A,             │  │
│  │  deliverable_service.py   and deliverable gen)              │  │
│  │                           — context-only, no web search    │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Knowledge Compilation Pipeline

The ingestion pipeline routes by file type. Spreadsheets (xlsx, xls, csv) take a structure-aware path; all other documents take the text-extraction path. Both paths converge at the graph update and pattern threshold check.

```
Upload (PDF / DOCX / PPTX / XLSX / XLS / CSV)
        │
        ▼
Magic-byte validation → file-type detection
  PDF  → PyMuPDF
  DOCX → python-docx
  PPTX → python-pptx
  XLSX / XLS → openpyxl / xlrd
  CSV  → csv.Sniffer (BOM-aware)
        │
        ▼
        ├── Spreadsheet path (xlsx / xls / csv)
        │       │
        │       ▼
        │   Parse workbook → SheetData[] → WorkbookData
        │   Analyse sheets → SheetAnalysis[] (type classification, domain hints)
        │   Detect tables  → DetectedTable[] (labelled, bounded)
        │   Extract schema → WorkbookSchema (structural prior for LLM)
        │   Per-table concept extraction (sheet-type-aware prompt)
        │   → Insert Concept rows  (source_document_id, source_excerpt from row)
        │   → Insert Relationship rows
        │   → Record SpreadsheetIngestionRun audit row
        │
        └── Document path (pdf / docx / pptx)
                │
                ▼
            Store raw_text on Document row
            ┌── Agent 1: Concept Compiler ──────────────────────────┐
            │  Chunked document text → LLM                          │
            │  Extracts: {name, type, description, source_excerpt}   │
            │  Pydantic validation, confidence filter               │
            │  Insert Concept rows                                  │
            └───────────────────────────────────────────────────────┘
                │
                ▼
            ┌── Agent 2: Relationship Compiler ─────────────────────┐
            │  Workspace concept list + document text → LLM         │
            │  Fuzzy name resolution → Concept IDs                 │
            │  Insert Relationship rows                             │
            └───────────────────────────────────────────────────────┘
        │
        ▼  (both paths converge here)
┌── Agent 3: Pattern Compiler (threshold-gated) ────────────────────┐
│  Runs only when workspace knowledge grew ≥ PATTERN_EXTRACTION_    │
│  THRESHOLD_PERCENT since the last run                             │
│  Full workspace Concepts + Relationships → LLM                   │
│  Upsert ConsultingPattern rows (by name within workspace)        │
│  Record PatternExtractionRun audit row                           │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
Update graph memory (incremental node/edge add)
Update Document: status=complete
(on exception: status=failed, error_message=str(e))
```

**Why this chained agent approach?**
Concept extraction is scoped to a single document. Relationship extraction needs the full workspace concept map to avoid hallucinating names. Pattern extraction needs the full workspace graph to find cross-document recurring structures. Each agent benefits from the previous one's output while remaining independently testable.

---

## 4. Evidence-Grounded Q&A Pipeline (Graph-First Retrieval)

```
User question → POST /workspaces/{id}/ask
        │
        ▼
1. Keyword extraction (LLM-assisted + token fallback)
        │
        ▼
2. Graph-first retrieval:
   Load workspace NetworkX graph (built from Concept + Relationship rows)
   Match concept nodes by keyword → expand N-hop neighbourhood (default: 2 hops)
        │
        ▼
3. Evidence assembly:
   Pull source_excerpt for every node in the subgraph
   Pull ConsultingPattern rows referencing matched concepts
   Assemble context window: {concept, description, excerpt, source doc}
        │
        ▼
4. LLM generates answer (Claude, context-only):
   System prompt: "Answer ONLY from the provided context.
   If context doesn't support the answer, say: not found in the uploaded knowledge.
   Cite every claim with [Source: Document Name]."
        │
        ▼
5. Validation: answer is non-"not found" AND sources is empty → reject, return not-found
        │
        ▼
6. Persist ChatMessage rows (user + assistant)
   Return: {answer, sources: [{document_name, excerpt}]}
```

**Why graph-first, not embeddings-first?**
Graph traversal surfaces structured relationships — ISO 20022 *implements* SWIFT *enables* real-time settlement — that embedding similarity cannot. For IBM Consulting use cases where the *connection between concepts* is the value, graph traversal produces better context than nearest-neighbour vector retrieval. Embeddings can be layered in as a fallback if keyword matching proves too sparse, but graph traversal is the primary retrieval mechanism.

---

## 5. Data Model

```
Workspace ─── Document ─── Concept ─── Relationship (Concept → Concept)
    │              │                        │
    │              └──────────────► source_document_id
    │
    ├── ConsultingPattern (related_concept_ids[], source_document_ids[])
    ├── Deliverable       (source_concept_ids[], source_document_ids[])
    └── ChatMessage       (source_document_ids[])
```

All JSON list fields (`topics`, `ibm_approach`, `related_concept_ids`, etc.) are stored as JSON text via a `TypeDecorator` — transparent on read/write, portable across SQLite and PostgreSQL.

All foreign keys are indexed. Cascade delete on workspace removes all associated rows.

---

## 6. Repository / Interface Layer

Every storage operation goes through a SQLAlchemy ORM query — no raw SQL. This means:
- SQLite → PostgreSQL: change `DATABASE_URL`, zero code changes required.
- NetworkX → Neo4j: implement `GraphRepository` with the same interface as `builder.py` + `traversal.py`.

---

## 7. MVP vs Phase 2 Architecture

| Component | MVP | Phase 2 |
|---|---|---|
| Database | SQLite | RDS PostgreSQL (same ORM) |
| Graph engine | NetworkX in-process | Amazon Neptune / Neo4j |
| Task queue | FastAPI BackgroundTasks | SQS + Celery worker fleet |
| File storage | Local disk (`./uploads`) | S3 |
| Deployment | `uvicorn` local | ECS Fargate + CloudFront |
| Auth | None | IBM SSO / Cognito |
| LLM | Anthropic direct API | Amazon Bedrock (Claude) |
| Vector search (optional) | None | pgvector / OpenSearch |

---

## 8. Non-Goals (Phase 2)

Authentication, multi-tenant isolation, CI/CD, production deployment, real-time collaborative editing, IBM enterprise system integrations (SharePoint, Teams, Confluence), document versioning.
