# Knowledge Intelligence Platform — API Reference

## Base URL
Development: `http://localhost:8000`

Interactive docs (Swagger UI): `http://localhost:8000/docs`
OpenAPI JSON: `http://localhost:8000/openapi.json`

---

## Core Design Principle: Grounding is Structurally Enforced

Every AI-generated endpoint (`/ask`, `/deliverables`) returns a `sources` array.
This is enforced by the `SourcedResponseMixin` in `schemas.py` — a Pydantic `model_validator` that runs before any response leaves the server. An answer without sources is a schema validation error, not just a prompt violation.

```json
"sources": [
  {
    "document_id": 12,
    "document_name": "ISO 20022 POV.pdf",
    "excerpt": "ISO 20022 is a financial messaging standard that enables richer data..."
  }
]
```

---

## Workspaces

### `POST /workspaces`
Create a new knowledge workspace.

**Request body:**
```json
{ "name": "Payments Modernization", "description": "Optional description" }
```
**Response 201:**
```json
{ "id": 1, "name": "Payments Modernization", "description": "...", "created_at": "...", "document_count": 0, "concept_count": 0 }
```

---

### `GET /workspaces`
List all workspaces, newest first. Includes `document_count` and `concept_count`.

---

### `GET /workspaces/{id}`
Get workspace detail. **404** if not found.

---

### `DELETE /workspaces/{id}`
Delete workspace and all associated knowledge (cascading delete). **204** on success, **404** if not found.

---

## Documents

### `POST /workspaces/{id}/documents`
Upload one or more documents. Compilation begins in a background task immediately after upload.

**Request:** `multipart/form-data`, field `files`. Accepted: PDF, DOCX, PPTX.

**Response 202:**
```json
[{ "id": 5, "workspace_id": 1, "filename": "...", "file_type": "pdf", "upload_status": "pending", "uploaded_at": "..." }]
```
**422** for unsupported file types.

---

### `GET /workspaces/{id}/documents`
List documents with per-document compilation status. Poll this endpoint to track progress.

**Status values:** `pending` → `processing` → `complete` | `failed`

---

### `GET /documents/{id}`
Full document detail including extracted `title`, `industry`, `topics[]`, and `raw_text`.

---

### `DELETE /documents/{id}`
Remove a document and its extracted knowledge. **204**.

---

## Knowledge Graph

### `GET /workspaces/{id}/concepts`
All concepts compiled from workspace documents.

```json
[{
  "id": 1, "name": "ISO 20022", "type": "Payment Standard",
  "description": "...", "source_document_id": 5,
  "source_excerpt": "ISO 20022 is a messaging standard..."
}]
```

---

### `GET /workspaces/{id}/relationships`
All concept relationships discovered across documents.

```json
[{ "id": 1, "source_concept_id": 1, "target_concept_id": 3, "relationship_type": "implements", "source_document_id": 5 }]
```

**Allowed `relationship_type` values:** `depends_on`, `requires`, `implements`, `extends`, `contrasts_with`, `enables`, `is_part_of`, `related_to`

---

### `GET /workspaces/{id}/patterns`
Consulting patterns identified from the workspace knowledge structure.

```json
[{
  "id": 1, "name": "ISO 20022 Migration Pattern",
  "problem_statement": "...",
  "ibm_approach": ["Step 1:...", "Step 2:..."],
  "related_concept_ids": [1, 2], "source_document_ids": [5]
}]
```

---

### `GET /workspaces/{id}/graph`
Full workspace knowledge graph in React Flow–compatible format.

```json
{
  "nodes": [{ "id": "1", "data": { "label": "ISO 20022", "type": "Payment Standard", "description": "..." }, "position": {"x": 120, "y": 80} }],
  "edges": [{ "id": "e1-3", "source": "1", "target": "3", "label": "implements", "data": { "strength": 0.9 } }]
}
```

---

### `GET /workspaces/{id}/graph/node/{node_id}?hops=2`
Node detail with N-hop neighbourhood for the side panel.

```json
{
  "node": { "...concept fields..." },
  "neighbours": [ "...concepts..." ],
  "edges": [ "...relationships..." ],
  "source_document": { "...document fields..." }
}
```

---

## Assistant

### `POST /workspaces/{id}/ask`
Ask a question. The assistant answers strictly from compiled workspace knowledge using graph-first retrieval.

**Request:**
```json
{ "question": "How does ISO 20022 relate to SWIFT?" }
```

**Response 200 — answered:**
```json
{
  "answer": "ISO 20022 implements the SWIFT messaging standard... [Source: ISO 20022 POV.pdf]",
  "sources": [{ "document_id": 5, "document_name": "ISO 20022 POV.pdf", "excerpt": "ISO 20022 provides richer data than MT messages..." }]
}
```

**Response 200 — not found:**
```json
{
  "answer": "I could not find this in the uploaded knowledge. The question may fall outside the documents in this workspace.",
  "sources": []
}
```

---

### `GET /workspaces/{id}/chat-history`
All chat messages for the workspace, ordered oldest-first.

---

## Deliverables

### `POST /workspaces/{id}/deliverables`
Generate a consulting deliverable from compiled workspace knowledge.

**Request:**
```json
{ "type": "POV", "topic": "ISO 20022 Migration", "audience": "CIO" }
```
**`type` values:** `POV`, `executive_summary`, `roadmap`

**Response 201:**
```json
{
  "deliverable": { "id": 3, "type": "POV", "title": "Point of View: ISO 20022 Migration", "content_markdown": "## Executive Summary\n...", "..." },
  "sources": [ "...source refs..." ]
}
```

---

### `GET /workspaces/{id}/deliverables`
List all deliverables in the workspace.

---

### `GET /deliverables/{id}`
Get a single deliverable.

---

### `PUT /deliverables/{id}`
Update deliverable content after manual editing.

**Request:** `{ "content_markdown": "..." }`

---

### `GET /deliverables/{id}/export?format=md|docx`
Export as Markdown text or DOCX binary. Returns file with `Content-Disposition` header.

---

## Health

### `GET /health`
Service health check.

**Response 200:**
```json
{ "status": "ok", "service": "Knowledge Intelligence Platform" }
```

---

## Error Responses

| Code | Meaning |
|---|---|
| 404 | Resource not found |
| 422 | Validation error (bad input, unsupported file type, or grounding rule violation) |
| 500 | Server error (LLM call failed, parsing error) |

All errors: `{ "detail": "human-readable message" }`
