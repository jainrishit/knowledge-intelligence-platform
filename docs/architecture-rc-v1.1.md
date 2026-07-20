# RC v1.1 Architecture Report
# Knowledge Intelligence Platform

**Status:** Release Candidate v1.1  
**Date:** June 2025  
**Classification:** Internal Engineering

---

## Executive Summary

The Knowledge Intelligence Platform is a full-stack consulting knowledge compiler and PowerPoint generation system. It transforms uploaded documents into a structured knowledge graph, enables evidence-grounded Q&A, and generates consultant-quality PowerPoint presentations through a plan → review → approve → generate workflow.

This document describes the RC v1.1 architecture as it actually exists in code — not as originally planned.

---

## System Overview

```
Browser (React + TypeScript)
        │
        │  HTTP/REST  Vite proxy → :8000
        ▼
FastAPI Application (Python 3.9+)
        │
        ├── Ingestion Pipeline
        │     parse → extract → graph
        │
        ├── Knowledge Graph (NetworkX + SQLAlchemy)
        │     concepts · relationships · patterns
        │     versioned cache (memory_manager.py)
        │
        ├── Retrieval Engine (qa_service.py)
        │     keyword extraction → BFS traversal → grounded answer
        │
        ├── Deliverable Generation Pipeline
        │     Phase 1 Blueprint → Phase 2 Review →
        │     Normalise → Phase 3 Validation →
        │     Phase 4 Conditional Review → Phase 5 PPTX
        │
        ├── Plan Workflow (plan_service.py)
        │     generate_plan → revise_plan → approve_and_generate
        │
        ├── Quality Assurance
        │     audit_service.py  ·  certification_service.py
        │
        └── LLM Governance
              circuit_breaker · rate_limiter · usage_tracker · retry
                    │
                    ▼
              Claude (IBM ICA endpoint)
```

---

## Data Model

All entities are namespaced per workspace. There is no cross-workspace data access at any layer.

| Model | Key fields | Notes |
|---|---|---|
| `Workspace` | `id`, `name`, `graph_version` | `graph_version` increments on doc add/delete; used to invalidate graph cache |
| `Document` | `workspace_id`, `filename`, `upload_status`, `raw_text` | Supports PDF, DOCX, PPTX, CSV, XLSX |
| `Concept` | `workspace_id`, `name`, `type`, `confidence`, `source_document_id`, `source_excerpt` | Confidence-filtered at extraction; verbatim excerpts always stored |
| `Relationship` | `workspace_id`, `source_concept_id`, `target_concept_id`, `relationship_type`, `strength` | Strength ≥ 0.35 (matches extraction floor) |
| `ConsultingPattern` | `workspace_id`, `name`, `problem_statement`, `ibm_approach`, `related_concept_ids` | Re-extracted when workspace grows ≥10% |
| `PresentationPlan` | `workspace_id`, `blueprint_json`, `status`, `revision_history`, `deliverable_id` | `status`: `draft` → `approved` |
| `Deliverable` | `workspace_id`, `type`, `title`, `source_concept_ids`, `source_document_ids` | Metadata record; PPTX bytes not stored |
| `LLMUsage` / `LLMBudget` | `workspace_id`, `tokens_in`, `tokens_out`, `cost_usd` | Per-call rows; running totals in budget |

---

## Ingestion Pipeline

```
Upload (PDF/DOCX/PPTX/CSV/XLSX)
        │
        ▼
Upload Validation (magic bytes + MIME allow-list + size limit)
        │
        ▼
Text Extraction (parsers.py)
  PyMuPDF · python-docx · python-pptx · csv_parser · excel_parser
        │
        ▼
Concept Agent (concept_agent.py)
  LLM extraction at temperature=0, confidence-scored, excerpt-attached
        │
        ▼
Relationship Agent (relationship_agent.py)
  Cross-concept link discovery, fuzzy name resolution, strength scoring
        │
        ▼
Pattern Agent (pattern_agent.py)  ← runs if workspace grew ≥10%
  Full-workspace pattern recognition, MECE clustering
        │
        ▼
Graph Invalidation
  graph_memory_manager.invalidate(workspace_id)
  workspace.graph_version += 1
```

**Document deletion cascade:** deleting a document removes its concepts, prunes all relationships sourced from those concepts or connecting to them, drops consulting patterns that lose all source documents, and invalidates the graph cache.

---

## Knowledge Graph Layer

The graph is a **derived layer** — it is not stored separately but rebuilt from the SQL rows on demand and then cached in-process.

```
SQL (Concept + Relationship rows)
        │
        ▼
graph/builder.py  →  NetworkX DiGraph (in-process)
        │
        ▼
graph/memory_manager.py
  Versioned LRU cache keyed on (workspace_id, graph_version)
  Invalidated on: document add, document delete, concept add/remove
        │
        ▼
graph/traversal.py
  find_nodes_by_keywords()  — multi-signal scoring (name, substring, type, description)
  get_neighbourhood()        — strength-filtered BFS (threshold 0.35, max 100 nodes)
```

**Key traversal parameters (config.py):**
- `GRAPH_STRENGTH_THRESHOLD = 0.35` — matches the relationship extraction floor; all stored relationships participate in retrieval
- `GRAPH_MAX_NODES = 100` — BFS ceiling; retrieval layer de-ranks low-relevance nodes before LLM context assembly
- `GRAPH_HOP_DEPTH = 2` — n-hop depth

---

## Retrieval / Q&A Pipeline

```
User question
        │
        ▼
Keyword extraction  (LLM, temperature=0)
        │
        ▼
Multi-signal concept scoring
  exact name match · substring · type · description
  weighted by concept confidence
        │
        ▼
Top-K seeds (QA_TOP_K = 30) → strength-filtered BFS
        │
        ▼
Deterministic sort  (seed relevance DESC, confidence DESC, id ASC)
        │
        ▼
Near-duplicate deduplication  (excerpt similarity)
        │
        ▼
Context assembly → Claude (temperature=0)
  Answer must cite [Source: Document Name] for each claim
        │
        ▼
SourcedResponseMixin validation
  Schema-level hard constraint: answer without sources → schema error
```

---

## Deliverable Generation Pipeline (Direct Path)

Used by `POST /workspaces/{id}/deliverables` and `GET /deliverables/{id}/export`.

```
Phase 1 — BLUEPRINT  (LLM call)
  System: _BLUEPRINT_SYSTEM_PROMPT  (annotation fields EXCLUDED)
  User: graph digest + deliverable type summary + slide count guidance
  max_tokens: 8192
  Output: blueprint dict

Phase 2 — QUALITY REVIEW  (LLM call)
  System: _REVIEW_SYSTEM_PROMPT
  User: Phase 1 blueprint JSON
  max_tokens: 8192
  Output: refined_blueprint dict (or original on failure)

Normalise  (zero LLM calls)
  _blueprint_to_deck_spec()
  Strips annotation fields, renumbers slides, populates source_documents

Phase 3 — VALIDATION  (deterministic, zero LLM calls)
  _validate_deck_spec()
  See Validation Layer section below

Phase 4 — CONDITIONAL PRESENTATION REVIEW  (LLM call, conditional)
  Triggered only if _residual_ending_issues() flags a content gap
  System: _PRESENTATION_REVIEW_SYSTEM_PROMPT
  max_tokens: 8192
  Followed by a second _validate_deck_spec() pass

Phase 5 — PPTX RENDER  (zero LLM calls)
  generate_pptx(deck_spec, workspace_name)
  IBM IPC_PPT_Template_2026.pptx
```

---

## Plan Workflow (Plan Path)

Used by the plan → review → approve → generate UI flow.

```
generate_plan()
  Phase 1 only
  System: _PLAN_BLUEPRINT_SYSTEM_PROMPT  (annotation fields REQUIRED)
  max_tokens: 24000  (larger budget for per-slide annotation fields)
  Defensive JSON: corrective retry on parse failure → deterministic fallback
  Stores: PresentationPlan (status=draft)

revise_plan()  [repeatable]
  System: _REVISION_SYSTEM_PROMPT  (annotation fields EXCLUDED from output)
  User: current blueprint + user instruction
  max_tokens: 16000
  Updates: PresentationPlan.blueprint_json, revision_history

update_plan_slides()  [local edits]
  Persists user reorder/remove/add edits to the blueprint
  No LLM call

approve_and_generate()
  Normalise → Phase 3 Validation → Phase 5 PPTX
  Phase 2 and Phase 4 are intentionally SKIPPED:
    the user has already reviewed and approved the plan.
  Marks PresentationPlan.status = "approved"
```

**Blueprint system prompt split:**
- `_BLUEPRINT_SYSTEM_PROMPT` (direct path) — forbids annotation fields to minimise output tokens
- `_PLAN_BLUEPRINT_SYSTEM_PROMPT` (plan path) — requires annotation fields (`purpose`, `key_insights`, `graph_concepts`, `relationships_used`, `patterns_used`, `evidence`) so the plan review UI can show graph grounding per slide

Annotation fields are stripped by `_blueprint_to_deck_spec()` before PPTX rendering — they never appear in the final deck.

---

## Validation Layer

`_validate_deck_spec()` runs before every PPTX render. Fully deterministic — zero LLM calls.

| Rule | Behaviour |
|---|---|
| Empty/placeholder slides | Removed (title-only, no content, or stray Sources slides) |
| `_RESTRICTED_LAYOUTS` rerouting | Stray static-diagram layouts rerouted to `title_content` |
| Layout diversity guard | No more than 2 consecutive identical layouts; 3rd+ rerouted via `_SIBLING_LAYOUT` |
| Section-divider hygiene | Label dividers rewritten to lead insight; dropped only if <2 following slides |
| Ending backstop | Swaps final weak slide with a recommendation slide if one exists |
| `process_diagram` minimum steps | <4 steps demoted to `title_content` |
| Bullet termination | Bullets without trailing period get one appended |

---

## LLM Governance Stack

```
Business logic
        │
        ▼
LLM Governance (governance.py)
  Budget check (monthly token + cost limit)
  Rate limit (60 req/min per workspace, sliding window)
        │
        ▼
Circuit Breaker (circuit_breaker.py)
  CLOSED → OPEN after 5 consecutive failures
  OPEN → HALF_OPEN after 60s recovery
        │
        ▼
llm_client.chat()
  _call_api_proxy() — thin proxy enabling unittest.mock.patch with Tenacity cache
        │
        ▼
Tenacity retry (retry.py)
  _is_transient(): httpx timeout/connect/protocol + anthropic 429/5xx
  stop_after_attempt(max_retries + 1)  = 4 total attempts
  wait_exponential(initial=5s, max=30s) + jitter
  reraise=False → returns last exception on exhaustion
        │
        ▼
Anthropic SDK (max_retries=0)
  SDK retries disabled — all retry logic lives in Tenacity layer
        │
        ▼
IBM ICA Claude endpoint
```

---

## API Surface

| Router | Prefix | Key endpoints |
|---|---|---|
| `workspaces` | `/workspaces` | CRUD workspaces |
| `documents` | `/workspaces/{id}/documents` | Upload, list, delete (cascade) |
| `graph` | `/workspaces/{id}/graph` | Graph nodes/edges, node neighbourhood |
| `assistant` | `/workspaces/{id}/ask` | Evidence-grounded Q&A, chat history |
| `plans` | `/workspaces/{id}/presentation-plans` | 6 plan workflow routes |
| `deliverables` | `/workspaces/{id}/deliverables` | Direct generate, list, re-export, delete |
| `audit` | `/workspaces/{id}/audit` | Workspace audit report + score |
| `certification` | `/workspaces/{id}/certification` | Run, status, report, gaps, history |
| `admin` | `/admin/llm` | Usage and budget visibility |

---

## Quality Assurance Systems

### Memory Audit (`audit_service.py`)
Scores the workspace knowledge for readiness:
- `ingestion_score` — document processing completeness
- `extraction_density_score` — concepts per document
- `relationship_density_score` — relationships per concept
- `evidence_coverage_score` — % concepts with verbatim excerpts
- `graph_integrity_score` — orphan concepts, multi-doc coverage
- `pattern_coverage_score` — patterns per concept cluster
- `consulting_readiness_score` — composite score
- `client_101_ready`, `client_201_ready`, `executive_summary_ready` — boolean readiness flags

### Brain Certification (`certification_service.py`)
Automated retrieval accuracy benchmarks:
1. LLM generates probe questions from workspace concepts
2. Questions are answered using the retrieval pipeline
3. Recall, precision, coverage, and consistency are scored
4. `certification_status`: CERTIFIED / PROVISIONAL / NOT_CERTIFIED
5. Knowledge gaps identified: concepts never retrieved, no evidence, orphaned

---

## Technology Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Graph engine | NetworkX (in-process) | Zero infrastructure; swap to Neptune/Neo4j via same ORM interface |
| Database | SQLite → PostgreSQL | Same SQLAlchemy ORM; change `DATABASE_URL` only |
| LLM retry | Tenacity (not SDK) | SDK double-retry storms prevented; transient-only predicate |
| Blueprint prompts split | Two separate system prompts | Plan path needs annotation fields; generation path optimises tokens |
| Conditional Phase 4 | `_residual_ending_issues()` gating | Eliminates LLM call (~12–30s) on decks the validator already leaves clean |
| No Phase 4 on plan path | Intentional skip | User has already reviewed and approved; silent post-approval changes violate the contract |
| Template | IPC_PPT_Template_2026.pptx | IBM brand compliance; IBM Asset Kit guidance superseded |
