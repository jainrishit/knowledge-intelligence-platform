# ADR: Payments Center Brain

**Status:** Proposed — not implemented  
**Deciders:** Knowledge Intelligence Platform team  

---

## Context

The Knowledge Intelligence Platform currently operates on a per-workspace model. Each workspace is an isolated knowledge domain: practitioners upload documents, the platform compiles them into a knowledge graph, and all retrieval and generation is scoped exclusively to that workspace.

This design is correct for project-specific and engagement-specific knowledge. However, it creates a gap for **practice-level institutional knowledge** — the accumulated domain expertise that belongs to an entire practice (e.g. the Payments Center of Excellence) rather than to any single engagement.

Today, this practice-level knowledge is either:
- Uploaded repeatedly into individual workspaces (duplicated effort, inconsistent across workspaces), or
- Not available to the platform at all (practitioners miss relevant institutional context when generating deliverables).

---

## Decision

We will design a **Payments Center Brain** — a centralised, shared institutional knowledge graph built from practice-wide assets and maintained by practice leads.

This is a **future enhancement**. It is documented here to preserve architectural intent and inform implementation decisions made before this feature is built.

---

## Proposed Design

### What it is

A shared, read-only knowledge graph scoped to the Payments Center of Excellence (and extensible to other practices). It is compiled from curated practice assets — canonical POVs, reference architectures, domain standards documents, methodology guides — and maintained by designated practice knowledge owners.

Unlike workspace knowledge graphs (which are project-specific and mutable by any workspace member), the Payments Center Brain is:
- **Shared** — visible to practitioners across all workspaces
- **Curated** — managed by practice leads, not individual project teams
- **Additive** — it supplements workspace knowledge, never replaces it

### Data sources

The Payments Center Brain will ingest from:
- SharePoint document libraries (Payments Center of Excellence repository)
- Box repositories (shared practice knowledge stores)
- Bulk-imported domain documents (standards, regulatory frameworks, methodology guides)

Ingestion uses the same three-agent compilation pipeline (Concept → Relationship → Pattern) already implemented for workspace documents.

### Knowledge scope selector

When a practitioner generates a deliverable (POV, BRD, PRD, Executive Summary, Roadmap), they will be offered a knowledge scope choice:

```
Knowledge Scope
───────────────
( ) Workspace Only
    Use only documents uploaded to this workspace.

(●) Workspace + Payments Center Brain
    Include institutional practice knowledge alongside workspace documents.
    Source citations will identify whether each claim came from workspace
    documents or from the shared practice knowledge base.
```

The default is **Workspace Only** to preserve the current behaviour and workspace isolation guarantee.

When **Workspace + Payments Center Brain** is selected:
- The retrieval pipeline queries both the workspace graph and the shared graph
- Retrieved concepts from both sources are merged and ranked together
- Source citations distinguish between workspace sources and shared knowledge sources
- The grounding rule applies equally to both sources — every claim must trace to a document

### Workspace behaviour is unchanged

Individual workspaces remain fully isolated from each other. The Payments Center Brain is an **opt-in additive layer**, not a shared context that is always present. Enabling it for one query does not affect other workspaces or other queries.

---

## Consequences

### Benefits
- Practitioners gain access to canonical institutional knowledge without uploading it to every workspace
- Practice leads can ensure consistent, authoritative information is available across all engagements
- Deliverables can synthesise both project-specific findings and practice-wide expertise in a single, source-cited output

### Risks and mitigations
- **Staleness:** Shared knowledge must be actively maintained. Implement a `last_reviewed` date on shared documents and surface a warning when knowledge is older than a configurable threshold.
- **Authority conflicts:** When workspace knowledge contradicts shared knowledge, the system must surface both views and let the practitioner decide. Never silently merge conflicting claims.
- **Access control:** The Payments Center Brain requires authentication and role-based access before it can be safely shared across practitioners. This feature is therefore blocked on authentication implementation.
- **Context explosion:** Combining two knowledge graphs risks assembling too much context. The existing `QA_TOP_K` and `GRAPH_MAX_NODES` caps apply independently to each source and must be configured appropriately.

---

## Implementation Prerequisites

Before this feature can be built:

1. **Authentication and authorisation** — practitioners must have verified identities before shared knowledge can be exposed
2. **SharePoint / Box connectors** — bulk ingestion from external repositories requires OAuth integration with those systems
3. **Multi-source retrieval** — the retrieval pipeline must support querying and merging results from two independent knowledge graphs
4. **Source provenance display** — the frontend must distinguish workspace citations from shared knowledge citations

---

## Out of Scope for this ADR

- Implementation timeline
- Specific SharePoint or Box API integration details
- Authentication mechanism selection
- Multi-practice generalisation (this ADR scopes to Payments Center; other practices are analogous but separate decisions)
