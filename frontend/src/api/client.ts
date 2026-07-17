import type {
  Workspace, Document, Concept, Relationship, ConsultingPattern,
  GraphOut, AskResponse, ChatMessage, Deliverable,
  DeliverableType, NodeNeighbourhood,
} from '@/types/api';

async function req<T>(url: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(detail.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

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

  assistant: {
    ask: (workspaceId: number, question: string) =>
      req<AskResponse>(`/workspaces/${workspaceId}/ask`, {
        method: 'POST',
        body: JSON.stringify({ question }),
      }),
    chatHistory: (workspaceId: number) =>
      req<ChatMessage[]>(`/workspaces/${workspaceId}/chat-history`),
  },

  deliverables: {
    /**
     * Generate a client material.
     * Returns a Blob (PPTX file) plus metadata headers.
     */
    create: async (
      workspaceId: number,
      type: DeliverableType,
      focus_area?: string,
    ): Promise<{ blob: Blob; filename: string; title: string; sourceCount: number }> => {
      const resp = await fetch(`/workspaces/${workspaceId}/deliverables`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, focus_area }),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(detail.detail || `HTTP ${resp.status}`);
      }
      const blob = await resp.blob();
      const cd = resp.headers.get('Content-Disposition') ?? '';
      const fnMatch = cd.match(/filename="([^"]+)"/);
      return {
        blob,
        filename: fnMatch?.[1] ?? `${type}.pptx`,
        title: resp.headers.get('X-Deliverable-Title') ?? type,
        sourceCount: parseInt(resp.headers.get('X-Source-Count') ?? '0', 10),
      };
    },
    list: (workspaceId: number) =>
      req<Deliverable[]>(`/workspaces/${workspaceId}/deliverables`),
    get: (id: number) => req<Deliverable>(`/deliverables/${id}`),
    /** Re-generate and download a saved deliverable as a fresh PPTX blob. */
    reExport: async (id: number): Promise<{ blob: Blob; filename: string }> => {
      const resp = await fetch(`/deliverables/${id}/export`);
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(detail.detail || `HTTP ${resp.status}`);
      }
      const blob = await resp.blob();
      const cd = resp.headers.get('Content-Disposition') ?? '';
      const fnMatch = cd.match(/filename="([^"]+)"/);
      return { blob, filename: fnMatch?.[1] ?? `deliverable_${id}.pptx` };
    },
    delete: (id: number) =>
      fetch(`/deliverables/${id}`, { method: 'DELETE' }).then(r => {
        if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
      }),
  },
};
