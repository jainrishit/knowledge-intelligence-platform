export interface Workspace {
  id: number;
  name: string;
  description: string | null;
  created_at: string;
  document_count: number;
  concept_count: number;
  relationship_count: number;
  pattern_count: number;
  graph_version: number;
  graph_last_updated: string | null;
}

export interface Document {
  id: number;
  workspace_id: number;
  filename: string;
  file_type: string;
  title: string | null;
  industry: string | null;
  topics: string[];
  upload_status: 'pending' | 'processing' | 'complete' | 'failed';
  error_message: string | null;
  uploaded_at: string;
}

export interface Concept {
  id: number;
  workspace_id: number;
  name: string;
  type: string | null;
  description: string | null;
  source_document_id: number | null;
  source_excerpt: string | null;
  created_at: string;
}

export interface Relationship {
  id: number;
  workspace_id: number;
  source_concept_id: number;
  target_concept_id: number;
  relationship_type: string;
  source_document_id: number | null;
  created_at: string;
}

export interface ConsultingPattern {
  id: number;
  workspace_id: number;
  name: string;
  problem_statement: string | null;
  ibm_approach: string[];
  related_concept_ids: number[];
  source_document_ids: number[];
  created_at: string;
}

export interface GraphNodeData {
  label: string;
  type: string;
  description: string;
  source_document_id: number | null;
  source_excerpt: string;
}

export interface GraphNode {
  id: string;
  data: GraphNodeData;
  position: { x: number; y: number };
  type: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  data: {
    relationship_type: string;
    source_document_id: number | null;
    strength: number;
    strokeWidth: number;
  };
}

export interface GraphOut {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface SourceRef {
  document_id: number;
  document_name: string;
  excerpt: string;
}

export interface AskResponse {
  answer: string;
  sources: SourceRef[];
}

export interface ChatMessage {
  id: number;
  workspace_id: number;
  role: 'user' | 'assistant';
  content: string;
  source_document_ids: number[];
  created_at: string;
}

export interface Deliverable {
  id: number;
  workspace_id: number;
  type: string;
  title: string;
  content_markdown: string | null;
  source_concept_ids: number[];
  source_document_ids: number[];
  created_at: string;
}

export type DeliverableType = 'client_101' | 'client_201' | 'executive_summary';

export interface NodeNeighbourhood {
  node: Concept;
  neighbours: Concept[];
  edges: Relationship[];
  source_document: Document | null;
}
