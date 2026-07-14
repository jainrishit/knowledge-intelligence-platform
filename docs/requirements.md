# Knowledge Intelligence Platform
## Product Requirements

---

## What is the Knowledge Intelligence Platform?

Organisations produce vast amounts of institutional knowledge every year — Points of View, BRDs, case studies, architecture patterns, delivery playbooks, industry research. But this knowledge is trapped in disconnected documents, dependent on individual experts, and rarely reused effectively. A practitioner starting a new engagement today has no reliable way to ask: *"Has this problem been solved before, and where is the evidence?"*

**Knowledge Intelligence Platform** is an evidence-grounded knowledge compiler. Practitioners upload existing knowledge assets into a workspace, and the platform transforms them into a structured, connected intelligence layer: concepts, relationships, consulting patterns, and evidence trails. Instead of searching through documents, practitioners get evidence-backed answers and client-ready deliverables — all traceable back to the organisation's own documented expertise.

**This is not a chatbot. This is not a document search tool.** This is a knowledge compiler — the intelligence engine that turns fragmented institutional knowledge into a connected, trustworthy, reusable asset. Every output shows its work: where it came from, and why it is relevant.

---

## 1. User Personas

### 1.1 Senior Consultant / Engagement Lead
**Profile:** 8–15 years experience. Owns client deliverables. Runs multiple engagements simultaneously.
**Pain points:** Rediscovers knowledge that exists elsewhere 30–40% of the time. Cannot quickly verify whether the organisation has solved this problem before.
**Goals:** Cite prior work authoritatively. Generate client-ready POVs in hours, not days. Answer client questions live with verified evidence.

### 1.2 Practice Lead / Knowledge Owner
**Profile:** Defines the organisation's approach on a practice domain (e.g. Payments, Digital Assets).
**Pain points:** Knowledge lives in flat file shares. Patterns are implicit and undiscovered across projects.
**Goals:** Compile a workspace per practice area. Discover emerging patterns. Generate up-to-date POVs that synthesise recent engagements.

### 1.3 New Hire / Onboarding Practitioner
**Profile:** 0–2 years experience. Needs to rapidly understand a practice area.
**Pain points:** Overwhelming document volume. No guided entry point. Afraid to make undocumented claims.
**Goals:** Ask plain-language questions and get evidence-backed answers. Explore the knowledge graph to understand how concepts connect. Produce first drafts quickly without reinventing content.

---

## 2. User Stories & Acceptance Criteria

### Feature 1 — Workspace Management

**US-1.1** As a practitioner, I want to create a named workspace to organise documents from a specific engagement or practice domain.
- **AC:** `POST /workspaces` succeeds with name and optional description; workspace appears in `GET /workspaces`.
- **AC:** Workspace names non-empty ≤ 200 chars.

**US-1.2** As a practice lead, I want to list all workspaces to navigate to the relevant domain quickly.
- **AC:** `GET /workspaces` returns all workspaces ordered newest-first with `document_count` and `concept_count`.

**US-1.3** As a practitioner, I want to delete a workspace to remove outdated engagement data.
- **AC:** `DELETE /workspaces/{id}` cascading-deletes all associated data. Non-existent ID → 404.

---

### Feature 2 — Upload & Compile

**US-2.1** As a practitioner, I want to drag-and-drop PDF, DOCX, or PPTX files into a workspace.
- **AC:** `POST /workspaces/{id}/documents` accepts multipart upload. Accepted: PDF, DOCX, PPTX.
- **AC:** Unsupported types → HTTP 422 with clear message.
- **AC:** Document row created immediately with `status=pending`.

**US-2.2** As a practitioner, I want to see compilation status per document.
- **AC:** `GET /workspaces/{id}/documents` returns per-document `upload_status`: `pending → processing → complete | failed`.
- **AC:** `failed` documents show a human-readable `error_message`.

**US-2.3** As a practitioner, I want to see compiled metadata for each processed document.
- **AC:** Once `status=complete`, `GET /documents/{id}` returns `title`, `industry`, `topics[]`. Fields nullable — system must not fail if extraction returns empty.

---

### Feature 3 — Knowledge Compilation

**US-3.1** As a practice lead, I want the platform to automatically compile concepts from uploaded documents.
- **AC:** After processing, `GET /workspaces/{id}/concepts` returns ≥1 concept per document (>500 words).
- **AC:** Every concept has non-null `source_document_id` and non-empty `source_excerpt`.
- **AC:** Concepts without traceable sources are rejected and not stored.

**US-3.2** As a practitioner, I want the platform to identify relationships between concepts across documents.
- **AC:** After ≥2 documents processed, `GET /workspaces/{id}/relationships` returns ≥1 relationship.
- **AC:** Every relationship references concept IDs that exist in the workspace.
- **AC:** `relationship_type` drawn from: `depends_on`, `requires`, `implements`, `extends`, `contrasts_with`, `enables`, `is_part_of`, `related_to`.

**US-3.3** As a practice lead, I want the platform to surface consulting patterns from the compiled knowledge.
- **AC:** `GET /workspaces/{id}/patterns` returns patterns with `name`, `problem_statement`, `ibm_approach[]`, ≥1 related concept, ≥1 source document.

---

### Feature 4 — Knowledge Graph Exploration

**US-4.1** As a practitioner, I want to explore workspace knowledge as an interactive graph.
- **AC:** `GET /workspaces/{id}/graph` returns React Flow–compatible `{nodes, edges}`.
- **AC:** Nodes: `id (str), data.label, data.type, data.description`. Edges: `id, source, target, label`.

**US-4.2** As a practitioner, I want to click a node and see its evidence and connected concepts.
- **AC:** `GET /workspaces/{id}/graph/node/{node_id}` returns node attributes, 2-hop neighbourhood, source document, and verbatim excerpt.

---

### Feature 5 — Evidence-Grounded Assistant

**US-5.1** As a practitioner, I want to ask questions about the workspace and get cited answers.
- **AC:** `POST /workspaces/{id}/ask` → `{answer, sources: [{document_name, excerpt}]}`.
- **AC:** `sources` non-empty whenever answer is non-"not found".
- **AC:** Validation layer (not just prompt) enforces the non-empty sources rule.

**US-5.2** As a practitioner, I want the assistant to explicitly say it cannot answer when the question is outside uploaded content.
- **AC:** Unrelated question → `answer` contains "not found in the uploaded knowledge"; `sources` is `[]`.

**US-5.3** As a practitioner, I want to scroll back through the workspace conversation history.
- **AC:** `GET /workspaces/{id}/chat-history` returns all messages ordered by `created_at` ascending.

---

### Feature 6 — Deliverable Generation

**US-6.1** As a practitioner, I want the platform to generate a client-ready POV, executive summary, or roadmap.
- **AC:** `POST /workspaces/{id}/deliverables` with `{type, topic, audience}` → deliverable with `content_markdown` and `sources[]`.
- **AC:** Generated document contains: Executive Summary, Context, Recommendation, Architecture Considerations, Roadmap, Risks.
- **AC:** Every section cites ≥1 source document if relevant content exists in the workspace.

**US-6.2** As a practitioner, I want to edit the generated deliverable before exporting.
- **AC:** `PUT /deliverables/{id}` with `{content_markdown}` updates the content.

**US-6.3** As a practitioner, I want to export the deliverable as Markdown or DOCX.
- **AC:** `GET /deliverables/{id}/export?format=md` → plain text Markdown.
- **AC:** `GET /deliverables/{id}/export?format=docx` → valid `.docx` binary.

---

## 3. Hard Constraints

- **Grounding rule (non-negotiable):** The platform must not rely on internet search, web browsing, or general/external knowledge for any extraction, answer, or deliverable. Every substantive output must be grounded exclusively in the uploaded workspace documents, with evidence and citations back to the source document and excerpt where extractable.
- **Workspace isolation:** No cross-workspace knowledge leakage in retrieval.
- **Source traceability:** Every compiled concept, relationship, and pattern carries `source_document_id` and `source_excerpt`.
- **SQLAlchemy ORM only:** No raw database-specific SQL in business logic.
- **Graph as derived layer:** NetworkX graph is rebuilt from SQL tables on demand. SQL is canonical.

---

## 4. Out of Scope (Future Phases)

- Authentication and authorisation
- Multi-tenant data isolation beyond workspace-level
- Production deployment and CI/CD
- Vector database integration (graph-first retrieval is primary)
- Real-time collaborative editing
- Enterprise system integrations (SharePoint, Box, Teams, Confluence)
- Document versioning
- Payments Center Brain shared knowledge layer (see ADR)
