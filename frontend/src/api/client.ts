// Typed API client — all calls proxy through Vite dev server to FastAPI at localhost:8000
import type {
  Workspace, Document, Concept, Relationship, ConsultingPattern,
  GraphOut, AskResponse, ChatMessage, Deliverable,
  DeliverableResponse, NodeNeighbourhood,
} from '@/types/api';

const BASE = '';

async function req<T>(url: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(detail.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// ── Workspaces ───────────────────────────────────────────────────────

export const api = {
  workspaces: {
    list: () => req<Workspace[]>('/workspaces'),
    create: (name: string, description?: string) =>
      req<Workspace>('/workspaces', {
        method: 'POST',
        body: JSON.stringify({ name, description }),
      }),
    get: (id: number) => req<Workspace>(`/workspaces/${id}`),
    delete: (id: number) =>
      fetch(`/workspaces/${id}`, { method: 'DELETE' }),
  },

  // ── Documents ────────────────────────────────────────────────────
  documents: {
    upload: (workspaceId: number, files: File[]) => {
      const form = new FormData();
      files.forEach(f => form.append('files', f));
      return fetch(`/workspaces/${workspaceId}/documents`, {
        method: 'POST',
        body: form,
      }).then(async r => {
        if (!r.ok) throw new Error((await r.json()).detail);
        return r.json() as Promise<Document[]>;
      });
    },
    list: (workspaceId: number) =>
      req<Document[]>(`/workspaces/${workspaceId}/documents`),
    get: (id: number) => req<Document>(`/documents/${id}`),
    delete: (id: number) => fetch(`/documents/${id}`, { method: 'DELETE' }),
  },

  // ── Knowledge ────────────────────────────────────────────────────
  knowledge: {
    concepts: (workspaceId: number) =>
      req<Concept[]>(`/workspaces/${workspaceId}/concepts`),
    relationships: (workspaceId: number) =>
      req<Relationship[]>(`/workspaces/${workspaceId}/relationships`),
    patterns: (workspaceId: number) =>
      req<ConsultingPattern[]>(`/workspaces/${workspaceId}/patterns`),
    graph: (workspaceId: number) =>
      req<GraphOut>(`/workspaces/${workspaceId}/graph`),
    nodeNeighbourhood: (workspaceId: number, nodeId: number) =>
      req<NodeNeighbourhood>(`/workspaces/${workspaceId}/graph/node/${nodeId}`),
  },

  // ── Assistant ─────────────────────────────────────────────────────
  assistant: {
    ask: (workspaceId: number, question: string) =>
      req<AskResponse>(`/workspaces/${workspaceId}/ask`, {
        method: 'POST',
        body: JSON.stringify({ question }),
      }),
    chatHistory: (workspaceId: number) =>
      req<ChatMessage[]>(`/workspaces/${workspaceId}/chat-history`),
  },

  // ── Deliverables ─────────────────────────────────────────────────
  deliverables: {
    create: (workspaceId: number, type: string, topic?: string, audience?: string) =>
      req<DeliverableResponse>(`/workspaces/${workspaceId}/deliverables`, {
        method: 'POST',
        body: JSON.stringify({ type, topic, audience }),
      }),
    list: (workspaceId: number) =>
      req<Deliverable[]>(`/workspaces/${workspaceId}/deliverables`),
    get: (id: number) => req<Deliverable>(`/deliverables/${id}`),
    update: (id: number, content_markdown: string) =>
      req<Deliverable>(`/deliverables/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ content_markdown }),
      }),
    exportUrl: (id: number, format: 'md' | 'docx') =>
      `/deliverables/${id}/export?format=${format}`,
  },
};
