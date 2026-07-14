# Bob Knowledge Fabric — System Architecture

---

## 1. Vision & Positioning

Bob Knowledge Fabric is an **AI-native knowledge compiler** for IBM Consulting.
It is not a chatbot, not a document search tool, and not a generic RAG pipeline.
Bob transforms fragmented institutional knowledge — POVs, BRDs, case studies, playbooks, architecture documents — into a structured, connected, evidence-traceable knowledge layer.

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
│  Assistant Chat  │  Deliverable Generator + Compare              │
└────────────────────────┬─────────────────────────────────────────┘
                         │  HTTP/REST (JSON) — Vite proxy → :8000
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│              BOB KNOWLEDGE FABRIC  —  FastAPI (Python)            │
│                                                                  │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────┐  │
│  │  Document        │  │  Knowledge       │  │  Graph Engine  │  │
│  │  Ingestion       │  │  Compiler (Bob)  │  │  NetworkX      │  │
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

Bob's extraction pipeline has three chained agents, each running after every document ingestion:

```
Upload (PDF / DOCX / PPTX)
        │
        ▼
File-type detection → route to parser
  PDF  → PyMuPDF
  DOCX → python-docx
  PPTX → python-pptx
        │
        ▼
Store Document row (status=processing, raw_text saved)
        │
        ▼
┌── Agent 1: Concept Compiler ─────────────────────────────────┐
│  Prompt Bob (Claude) with chunked document text              │
│  Extracts: {name, type, description, source_excerpt}         │
│  source_excerpt = verbatim quote from document text          │
│  Pydantic validation → reject any item missing source_excerpt│
│  Insert Concept rows (source_document_id = this doc)         │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌── Agent 2: Relationship Compiler ────────────────────────────┐
│  Load all workspace Concepts                                 │
│  Prompt Bob with concept list + document text                │
│  Extracts: {source, target, relationship_type}               │
│  Fuzzy-match concept names → resolve to Concept IDs         │
│  Only accept relationships where both concepts exist in DB   │
│  Insert Relationship rows                                    │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌── Agent 3: Pattern Compiler ─────────────────────────────────┐
│  Load workspace Concepts + Relationships                     │
│  Prompt Bob with full workspace knowledge context            │
│  Extracts: {name, problem_statement, ibm_approach[], ...}    │
│  Upsert ConsultingPattern rows (by name within workspace)    │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
Update Document: status=complete
(on exception: status=failed, error_message=str(e))
```

**Why this chained agent approach?**
Concept extraction is scoped to a single document — it only needs that document's text.
Relationship extraction needs the full workspace concept map to avoid hallucinating concept names.
Pattern extraction needs the full workspace graph — it looks for cross-document recurring structures.
Chaining these three agents lets each one benefit from the work of the previous while keeping the extraction logic separated and testable.

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
4. Bob generates answer (Claude, context-only):
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
Graph traversal surfaces structured relationships — ISO 20022 *implements* SWIFT *enables* real-time settlement — that embedding similarity cannot. For IBM Consulting use cases where the *connection between concepts* is the value, graph traversal produces better context than nearest-neighbour vector retrieval. Embeddings can be layered in as a fallback if keyword matching proves too sparse, but graph traversal is the demo-differentiating mechanism.

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
