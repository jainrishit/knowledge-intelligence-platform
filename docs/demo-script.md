# Knowledge Intelligence Platform — Demo Script

## Pre-Demo Setup (5 minutes before)

```bash
# Terminal 1 — Backend
cd backend
source venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Terminal 2 — Frontend
cd frontend
npm run dev
```

Open **http://localhost:5173**. Have 3–4 documents ready: a domain POV, a standards overview, a case study or architecture document, and a research brief.

---

## Demo Flow (~18 minutes)

---

### Scene 1 — Frame the Problem (0:00–1:30)

*"Every organisation produces thousands of valuable assets every year — Points of View, case studies, playbooks, architecture documents. But this knowledge is locked in flat files. A practitioner starting an engagement today has no reliable way to ask: has this been solved before? And if so, where is the evidence?*

*The Knowledge Intelligence Platform is the answer. Not a chatbot. Not document search. A knowledge compiler — it reads your assets and turns them into a structured, connected, evidence-traceable intelligence layer. Every claim cites its source. It shows its work."*

---

### Scene 2 — Create Workspaces (1:30–3:00)

- Click **+ New Workspace** → name it **"Payments Modernization"** → Create
- Create a second workspace: **"Digital Assets"**
- Show the workspace card grid — document count and concept count both start at 0

*"Workspaces are the unit of isolation. A Payments engagement never sees Digital Assets data. That isolation is enforced at the database level — this is not a RAG system where context leaks between topics."*

---

### Scene 3 — Upload & Watch the Platform Compile (3:00–7:00)

- Click into **"Payments Modernization"**
- Drag and drop 3–4 documents into the Document Library
- Watch the status badges: **Queued → Processing → Ready**
- Once complete, show the pipeline explainer: text extraction → concept detection → relationship mapping → pattern recognition

*"The platform has read these documents. It has extracted every named concept, discovered how they relate to each other, and identified recurring patterns — all automatically, all traceable."*

---

### Scene 4 — Explore the Knowledge Graph (7:00–10:00)

- Click **Knowledge Graph** tab
- Wait for the canvas to render: nodes = concepts, edges = relationships
- Point to specific nodes: "This is ISO 20022 — a Payment Standard. This edge says it implements SWIFT messaging."
- Click a node → side panel opens
  - Show: concept type, description, verbatim source excerpt, source document name
  - Show: connected concepts and relationship labels

*"Every node traces back to a specific sentence in a specific document. There is no invented knowledge here. This is the graph — not a list of search results. The platform understands that ISO 20022 implements SWIFT, which enables real-time settlement. That structure is what makes the next two features powerful."*

---

### Scene 5 — Ask the Assistant a Question (10:00–13:00)

- Click **Assistant** tab
- Ask: **"What are the key benefits of ISO 20022 over older messaging standards?"**
  - Show the answer with inline source citations
  - Expand a citation: show document name and verbatim excerpt
  - Point out: the answer only contains claims the documents support
- Ask: **"What is IBM's position on quantum computing in payment modernization?"**
  - Show the **"not found in the uploaded knowledge"** response — sources list is empty

*"This is the grounding rule in action. The system prompt says: answer only from the provided context. More importantly, our validation layer enforces it — if the assistant tries to return an answer with no sources, the API rejects it before it reaches the interface. The guarantee is structural, not a matter of trust."*

---

### Scene 6 — Generate a Deliverable and Export (13:00–18:00)

- Click **Deliverables** tab
- Select: **Type = "Point of View"**, **Topic = "ISO 20022 Migration"**, **Audience = "CIO"**
- Click **Generate** → ~10 second wait
- Show the generated Markdown: Executive Summary, Context, Recommendation, Architecture Considerations, Roadmap, Risks
- Edit one sentence in the editor (showing it is live-editable)
- Click **Save**
- Click **.docx** → open the exported file

*"From uploaded document to client-ready Point of View in under a minute. Every section cites its source. This is the deliverable accelerator — not to replace practitioner judgment, but to give them a grounded first draft they can trust and refine."*

---

### Scene 7 — Wrap & Roadmap (18:00–20:00)

| What is built (current) | What comes next |
|---|---|
| Graph-first retrieval | Amazon Neptune / Neo4j for enterprise-scale |
| SQLite + SQLAlchemy | RDS PostgreSQL (connection string swap) |
| FastAPI BackgroundTasks | SQS + Celery worker fleet |
| Local file storage | S3 |
| Local deployment | ECS Fargate + CloudFront |
| No authentication | IBM SSO / Cognito |
| ICA Claude endpoint | Amazon Bedrock (Claude) |
| Per-workspace knowledge | Payments Center Brain (shared institutional graph) |

*"The platform is production-architecture-ready. The interfaces are designed for swap-in replacement — change DATABASE_URL to PostgreSQL and you are there. Add a Neptune adapter behind the graph interface and you are on enterprise graph infrastructure.*

*The hard part — getting knowledge compilation right, getting the grounding rule enforced structurally, making retrieval deterministic and consistent — that is done.*

*The point of this platform is trust. Every answer, every pattern, every deliverable traces back to documented expertise. That is a different kind of AI — one you can put in front of a client."*
