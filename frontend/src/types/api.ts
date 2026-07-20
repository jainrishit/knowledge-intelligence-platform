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
  strength: number;
  reasoning: string | null;
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
    reasoning: string | null;
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

export interface PlanSlide {
  slide_number: number;
  title: string;
  layout: string;
  purpose: string | null;
  section: string | null;
  bullets: string[];
  columns: string[][] | null;
  col_heads: string[] | null;
  boxes: string[] | null;
  stats: Array<{ label: string; body: string }> | null;
  notes: string | null;
  visual_recommendation: string | null;
  // Graph intelligence annotation fields — populated by Claude during planning.
  // key_insights: the "so what" consulting takeaways shown during plan review.
  key_insights: string[];
  graph_concepts: string[];
  relationships_used: string[];
  patterns_used: string[];
  evidence: string[];
}

export interface RevisionRecord {
  instruction: string;
  changes: string[];
  timestamp: string;
}

export interface GraphCoverage {
  concepts_available: number;
  relationships_analyzed: number;
  patterns_available: number;
  source_documents: number;
  concepts_selected: number;
}

export interface PresentationPlan {
  id: number;
  workspace_id: number;
  deliverable_type: DeliverableType;
  focus_area: string | null;
  graph_coverage: GraphCoverage | null;
  slides: PlanSlide[];
  governing_messages: string[];
  storyline_summary: string | null;
  deck_title: string | null;
  revision_history: RevisionRecord[];
  status: 'draft' | 'approved';
  deliverable_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentAuditRecord {
  document_id: number;
  document_title: string;
  file_type: string;
  upload_status: string;
  char_count: number;
  chunk_count: number;
  page_count: number;
  concepts_extracted: number;
  relationships_extracted: number;
  concepts_with_evidence: number;
  evidence_coverage_pct: number;
  ingestion_started_at: string | null;
  ingestion_completed_at: string | null;
  ingestion_duration_seconds: number | null;
  error_detail: string | null;
  document_health_score: number;
}

export interface GraphIntegritySummary {
  total_concepts: number;
  total_relationships: number;
  // Legacy fields — now represent unconnected_candidates (kept for API compat)
  orphan_concepts: number;
  orphan_pct: number;
  // Concept classification
  connected_concepts: number;
  standalone_concepts: number;
  unconnected_candidates: number;
  relationship_coverage_pct: number;
  // Relationship quality
  unique_relationship_types: number;
  avg_relationship_strength: number;
  // Confidence
  multi_doc_concepts: number;
  multi_doc_pct: number;
  avg_confidence: number;
  low_confidence_concepts: number;
}

export interface WorkspaceAuditReport {
  workspace_id: number;
  workspace_name: string;
  graph_version: number;
  generated_at: string;

  total_documents: number;
  complete_documents: number;
  failed_documents: number;
  pending_processing_documents: number;

  total_chars_ingested: number;
  total_chunks_processed: number;
  total_pages_processed: number;

  total_concepts: number;
  total_relationships: number;
  total_patterns: number;
  concepts_with_evidence: number;
  evidence_coverage_pct: number;

  integrity: GraphIntegritySummary;

  ingestion_score: number;
  extraction_density_score: number;
  relationship_density_score: number;
  evidence_coverage_score: number;
  graph_integrity_score: number;
  pattern_coverage_score: number;
  consulting_readiness_score: number;
  relationship_coverage_score: number;
  memory_confidence_score: number;

  client_101_ready: boolean;
  client_201_ready: boolean;
  executive_summary_ready: boolean;

  documents: DocumentAuditRecord[];
}

export interface WorkspaceAuditScore {
  workspace_id: number;
  graph_version: number;
  memory_confidence_score: number;
  ingestion_score: number;
  extraction_density_score: number;
  relationship_density_score: number;
  evidence_coverage_score: number;
  graph_integrity_score: number;
  pattern_coverage_score: number;
  consulting_readiness_score: number;
  client_101_ready: boolean;
  client_201_ready: boolean;
  executive_summary_ready: boolean;
  total_documents: number;
  complete_documents: number;
  failed_documents: number;
  total_concepts: number;
  total_relationships: number;
  total_patterns: number;
  evidence_coverage_pct: number;
  generated_at: string;
}

export interface NodeNeighbourhood {
  node: Concept;
  neighbours: Concept[];
  edges: Relationship[];
  source_document: Document | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Brain Certification
// ─────────────────────────────────────────────────────────────────────────────

export interface QuestionResult {
  question_id: number;
  question_text: string;
  question_type: string;
  ground_truth_ids: number[];
  retrieved_ids: number[];
  has_evidence: boolean;
  recall: number;
  precision: number;
}

export interface KnowledgeGap {
  concept_id: number;
  concept_name: string;
  concept_type: string;
  gap_type: 'never_retrieved' | 'no_evidence' | 'orphaned';
  source_document_id: number | null;
}

export interface RetrievalCertificationReport {
  workspace_id: number;
  workspace_name: string;
  run_id: number;
  graph_version: number;
  generated_at: string;

  recall_score: number;
  precision_score: number;
  coverage_score: number;
  consistency_score: number;
  evidence_fidelity: number;
  retrieval_accuracy: number;

  certification_status: 'CERTIFIED' | 'PROVISIONAL' | 'NOT_CERTIFIED';
  certification_score: number;

  questions_generated: number;
  questions_answered: number;
  total_workspace_concepts: number;
  concepts_covered: number;
  concepts_never_retrieved: number;

  question_results: QuestionResult[];
  knowledge_gaps: KnowledgeGap[];

  recall_pass: boolean;
  precision_pass: boolean;
  coverage_pass: boolean;
  consistency_pass: boolean;
  evidence_pass: boolean;
}

export interface CertificationStatus {
  workspace_id: number;
  run_id: number | null;
  certification_status: 'CERTIFIED' | 'PROVISIONAL' | 'NOT_CERTIFIED' | 'PENDING' | 'RUNNING';
  certification_score: number;
  recall_score: number;
  precision_score: number;
  coverage_score: number;
  consistency_score: number;
  evidence_fidelity: number;
  retrieval_accuracy: number;
  questions_generated: number;
  questions_answered: number;
  knowledge_gap_count: number;
  graph_version: number;
  completed_at: string | null;
}

export interface CertificationRunSummary {
  run_id: number;
  status: string;
  certification_status: string;
  certification_score: number;
  recall_score: number;
  precision_score: number;
  coverage_score: number;
  consistency_score: number;
  evidence_fidelity: number;
  retrieval_accuracy: number;
  questions_generated: number;
  questions_answered: number;
  graph_version: number;
  started_at: string | null;
  completed_at: string | null;
}
