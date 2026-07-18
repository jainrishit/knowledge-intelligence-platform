import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '@/api/client';
import type { WorkspaceAuditReport, DocumentAuditRecord } from '@/types/api';
import {
  AlertCircle, CheckCircle2, ChevronDown, ChevronUp,
  Clock, FileText, Loader2, RefreshCw,
} from 'lucide-react';

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function scoreLabel(score: number): string {
  if (score >= 85) return 'Strong';
  if (score >= 70) return 'Good';
  if (score >= 55) return 'Moderate';
  if (score >= 40) return 'Limited';
  return 'Insufficient';
}

function confidenceLabel(score: number): string {
  if (score >= 85) return 'High Confidence';
  if (score >= 65) return 'Moderate Confidence';
  if (score >= 40) return 'Low Confidence';
  return 'Insufficient Data';
}

function readinessLabel(r101: boolean, r201: boolean, rExec: boolean): string {
  if (r101 && r201 && rExec) return 'All deliverable types ready';
  if (r101 || rExec) return 'Partial deliverable readiness';
  return 'Not ready for deliverable generation';
}

// ─────────────────────────────────────────────────────────────────────────────
// MetricRow — single KPI row with label, value, and thin bar
// ─────────────────────────────────────────────────────────────────────────────

function MetricRow({
  label,
  score,
  description,
}: {
  label: string;
  score: number;
  description: string;
}) {
  return (
    <div className="py-3.5 border-b last:border-b-0">
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-sm text-foreground">{label}</span>
        <div className="flex items-baseline gap-2">
          <span className="text-xs text-muted-foreground">{scoreLabel(score)}</span>
          <span className="text-sm font-semibold tabular-nums text-foreground w-8 text-right">
            {score.toFixed(0)}
          </span>
        </div>
      </div>
      <div className="h-px w-full bg-foreground/[0.08]">
        <div
          className="h-full bg-foreground/50 transition-all duration-300"
          style={{ width: `${Math.min(100, score)}%` }}
        />
      </div>
      <p className="text-[11px] text-muted-foreground mt-1.5 leading-relaxed">{description}</p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// ReadinessList — deliverable readiness checks
// ─────────────────────────────────────────────────────────────────────────────

function ReadinessList({
  client101, client201, execSummary,
}: { client101: boolean; client201: boolean; execSummary: boolean }) {
  const items = [
    { label: 'Client 101', ready: client101, desc: 'Full client briefing for new engagement teams' },
    { label: 'Client 201', ready: client201, desc: 'Deep analysis for experienced teams' },
    { label: 'Executive Summary', ready: execSummary, desc: 'Leadership briefing on a focused topic' },
  ];
  return (
    <div className="divide-y">
      {items.map(({ label, ready, desc }) => (
        <div key={label} className="flex items-center gap-3 py-3">
          {ready
            ? <CheckCircle2 className="w-4 h-4 text-foreground flex-shrink-0" />
            : <div className="w-4 h-4 rounded-full border-2 border-foreground/20 flex-shrink-0" />
          }
          <div className="flex-1 min-w-0">
            <p className={`text-sm font-medium ${ready ? 'text-foreground' : 'text-muted-foreground'}`}>
              {label}
            </p>
            <p className="text-[11px] text-muted-foreground">{desc}</p>
          </div>
          <span className={`text-[11px] font-medium ${ready ? 'text-foreground' : 'text-muted-foreground'}`}>
            {ready ? 'Ready' : 'Needs more data'}
          </span>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Document row
// ─────────────────────────────────────────────────────────────────────────────

function DocumentRow({ doc }: { doc: DocumentAuditRecord }) {
  const [expanded, setExpanded] = useState(false);

  const statusMark =
    doc.upload_status === 'complete'    ? <CheckCircle2 className="w-3.5 h-3.5 text-foreground flex-shrink-0" /> :
    doc.upload_status === 'failed'      ? <AlertCircle  className="w-3.5 h-3.5 text-foreground/40 flex-shrink-0" /> :
    doc.upload_status === 'processing'  ? <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground flex-shrink-0" /> :
    <Clock className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />;

  return (
    <div className="border-b last:border-b-0">
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center gap-3 px-5 py-3.5 text-left hover:bg-foreground/[0.02] transition-colors"
      >
        {statusMark}
        <div className="flex-1 min-w-0">
          <p className="text-sm text-foreground truncate">{doc.document_title}</p>
          <p className="text-[11px] text-muted-foreground mt-0.5">
            {doc.file_type.toUpperCase()}
            {doc.page_count > 0 && ` · ${doc.page_count} pages`}
          </p>
        </div>
        <div className="flex items-center gap-6 flex-shrink-0 text-[11px] text-muted-foreground">
          <span>{doc.concepts_extracted} concepts</span>
          <span>{doc.relationships_extracted} relationships</span>
          <span className="font-semibold text-foreground w-6 text-right tabular-nums">
            {doc.document_health_score.toFixed(0)}
          </span>
        </div>
        {expanded
          ? <ChevronUp className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
          : <ChevronDown className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
        }
      </button>

      {expanded && (
        <div className="px-12 pb-4 pt-1 grid grid-cols-3 gap-4 text-[11px] text-muted-foreground bg-foreground/[0.01]">
          <div>
            <p className="text-foreground font-medium">{doc.char_count.toLocaleString()}</p>
            <p>characters ingested</p>
          </div>
          <div>
            <p className="text-foreground font-medium">{doc.chunk_count}</p>
            <p>text chunks extracted</p>
          </div>
          <div>
            <p className="text-foreground font-medium">
              {doc.evidence_coverage_pct.toFixed(0)}%
            </p>
            <p>evidence coverage ({doc.concepts_with_evidence} of {doc.concepts_extracted} concepts)</p>
          </div>
          {doc.ingestion_duration_seconds !== null && (
            <div>
              <p className="text-foreground font-medium">{doc.ingestion_duration_seconds.toFixed(1)}s</p>
              <p>ingestion time</p>
            </div>
          )}
          {doc.error_detail && (
            <div className="col-span-3 px-3 py-2 border bg-foreground/[0.02] text-foreground">
              {doc.error_detail}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main page
// ─────────────────────────────────────────────────────────────────────────────

export default function MemoryAudit() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);

  const [report, setReport] = useState<WorkspaceAuditReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const r = await api.audit.workspace(wsId);
      setReport(r);
      setLastRefreshed(new Date());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Audit failed.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [wsId]);

  if (loading && !report) {
    return (
      <main className="max-w-4xl mx-auto px-8 py-12">
        <div className="py-20 flex flex-col items-center gap-3 text-muted-foreground">
          <Loader2 className="w-6 h-6 animate-spin" />
          <p className="text-sm">Computing knowledge health…</p>
        </div>
      </main>
    );
  }

  if (error && !report) {
    return (
      <main className="max-w-4xl mx-auto px-8 py-12">
        <div className="callout callout-amber text-sm">{error}</div>
      </main>
    );
  }

  if (!report) return null;

  const { integrity } = report;
  const overallLabel = confidenceLabel(report.memory_confidence_score);
  const readyLabel   = readinessLabel(report.client_101_ready, report.client_201_ready, report.executive_summary_ready);

  return (
    <main className="max-w-4xl mx-auto px-8 py-10 space-y-10">

      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div className="pb-6 border-b flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Knowledge Health</h1>
          <p className="text-sm text-muted-foreground mt-1.5 leading-relaxed max-w-xl">
            Completeness, evidence coverage, and consulting readiness of this workspace.
            All metrics are derived from ingestion and extraction data — no estimates.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1.5 text-xs border px-3 py-1.5 hover:bg-muted transition-colors disabled:opacity-40 flex-shrink-0"
        >
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          Refresh
        </button>
      </div>

      {lastRefreshed && (
        <p className="text-[11px] text-muted-foreground -mt-8">
          Computed {lastRefreshed.toLocaleTimeString()} · Graph v{report.graph_version} ·{' '}
          {report.total_documents} source {report.total_documents === 1 ? 'document' : 'documents'}
        </p>
      )}

      {/* ── Headline: Memory Confidence ──────────────────────────────────── */}
      <div className="border bg-white px-7 py-6">
        <div className="flex items-baseline justify-between mb-1">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
            Memory Confidence
          </h2>
          <span className="text-[11px] text-muted-foreground">Graph v{report.graph_version}</span>
        </div>
        <div className="flex items-end gap-5 mt-3">
          <div>
            <p className="text-6xl font-bold tabular-nums text-foreground leading-none">
              {report.memory_confidence_score.toFixed(0)}
            </p>
            <p className="text-xs text-muted-foreground mt-1">out of 100</p>
          </div>
          <div className="flex-1 pb-2">
            <p className="text-base font-semibold text-foreground mb-1">{overallLabel}</p>
            <p className="text-xs text-muted-foreground leading-relaxed max-w-md">
              Weighted composite of ingestion completeness, extraction density, evidence coverage,
              relationship depth, graph integrity, and pattern coverage.
            </p>
            <p className="text-xs text-muted-foreground mt-2">{readyLabel}</p>
          </div>
        </div>
        {/* Thin composite bar */}
        <div className="mt-5 h-1 w-full bg-foreground/[0.08]">
          <div
            className="h-full bg-foreground transition-all duration-500"
            style={{ width: `${Math.min(100, report.memory_confidence_score)}%` }}
          />
        </div>
      </div>

      {/* ── Two columns: quality metrics + knowledge volume ──────────────── */}
      <div className="grid grid-cols-2 gap-8">

        {/* Quality dimensions */}
        <div className="border bg-white px-5 py-5">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1 pb-3 border-b">
            Quality Dimensions
          </h2>
          <MetricRow
            label="Ingestion Completeness"
            score={report.ingestion_score}
            description="Fraction of documents fully processed. Failures reduce this score."
          />
          <MetricRow
            label="Knowledge Coverage"
            score={report.extraction_density_score}
            description="Concepts extracted per document — measures how thoroughly each source was analysed."
          />
          <MetricRow
            label="Relationship Depth"
            score={report.relationship_density_score}
            description="Connections per connected concept — standalone reference concepts are excluded from the denominator."
          />
          <MetricRow
            label="Relationship Coverage"
            score={report.relationship_coverage_score}
            description="Fraction of concepts expected to have relationships that are actually connected. Unconnected candidates reduce this score."
          />
          <MetricRow
            label="Evidence Coverage"
            score={report.evidence_coverage_score}
            description="Concepts with traceable source excerpts. Required for grounded deliverables."
          />
          <MetricRow
            label="Graph Connectivity"
            score={report.graph_integrity_score}
            description="Composite of relationship coverage, type diversity, and average relationship strength. Does not penalise standalone reference concepts."
          />
          <MetricRow
            label="Consulting Readiness"
            score={report.consulting_readiness_score}
            description="Composite of evidence, relationship depth, and pattern availability."
          />
        </div>

        {/* Knowledge volume + deliverable readiness */}
        <div className="space-y-6">

          {/* Volume counts */}
          <div className="border bg-white px-5 py-5">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1 pb-3 border-b">
              Knowledge Volume
            </h2>
            <div className="grid grid-cols-3 gap-4 text-center py-3">
              <div>
                <p className="text-3xl font-bold tabular-nums text-foreground">{report.total_concepts.toLocaleString()}</p>
                <p className="text-[11px] text-muted-foreground mt-1">concepts</p>
              </div>
              <div>
                <p className="text-3xl font-bold tabular-nums text-foreground">{report.total_relationships.toLocaleString()}</p>
                <p className="text-[11px] text-muted-foreground mt-1">relationships</p>
              </div>
              <div>
                <p className="text-3xl font-bold tabular-nums text-foreground">{report.total_patterns}</p>
                <p className="text-[11px] text-muted-foreground mt-1">patterns</p>
              </div>
            </div>
            <div className="border-t pt-3 space-y-1.5 text-[11px] text-muted-foreground">
              <p>
                <span className="font-medium text-foreground">{report.concepts_with_evidence.toLocaleString()}</span> of{' '}
                {report.total_concepts} concepts have traceable evidence{' '}
                ({report.evidence_coverage_pct.toFixed(0)}%)
              </p>
              {report.total_pages_processed > 0 && (
                <p>
                  <span className="font-medium text-foreground">{report.total_pages_processed.toLocaleString()}</span> pages ·{' '}
                  <span className="font-medium text-foreground">{report.total_chunks_processed.toLocaleString()}</span> text chunks
                </p>
              )}
              <p>
                <span className="font-medium text-foreground">{(report.total_chars_ingested / 1000).toFixed(0)}K</span>{' '}
                characters ingested
              </p>
            </div>
          </div>

          {/* Graph health */}
          <div className="border bg-white px-5 py-5">
            <div className="flex items-baseline justify-between pb-3 border-b mb-1">
              <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                Knowledge Connectivity
              </h2>
              <span className="text-[11px] text-muted-foreground tabular-nums">
                {report.graph_integrity_score.toFixed(0)} / 100
              </span>
            </div>

            {/* Concept classification */}
            <div className="grid grid-cols-3 gap-3 py-3 border-b">
              <div className="text-center">
                <p className="text-2xl font-bold tabular-nums text-foreground">{integrity.connected_concepts}</p>
                <p className="text-[11px] text-muted-foreground mt-0.5">connected</p>
                <p className="text-[10px] text-muted-foreground">in ≥1 relationship</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold tabular-nums text-foreground">{integrity.standalone_concepts}</p>
                <p className="text-[11px] text-muted-foreground mt-0.5">standalone</p>
                <p className="text-[10px] text-muted-foreground">reference concepts</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold tabular-nums text-foreground">{integrity.unconnected_candidates}</p>
                <p className="text-[11px] text-muted-foreground mt-0.5">unconnected</p>
                <p className="text-[10px] text-muted-foreground">missing links</p>
              </div>
            </div>

            {/* Relationship quality */}
            <div className="grid grid-cols-2 gap-4 py-3 border-b">
              <div>
                <p className="text-2xl font-bold tabular-nums text-foreground">
                  {integrity.relationship_coverage_pct.toFixed(0)}%
                </p>
                <p className="text-[11px] text-muted-foreground mt-0.5">relationship coverage</p>
                <p className="text-[10px] text-muted-foreground">connected / (connected + unconnected)</p>
              </div>
              <div>
                <p className="text-2xl font-bold tabular-nums text-foreground">
                  {integrity.unique_relationship_types}
                </p>
                <p className="text-[11px] text-muted-foreground mt-0.5">relationship types</p>
                <p className="text-[10px] text-muted-foreground">
                  avg. strength {(integrity.avg_relationship_strength * 100).toFixed(0)}%
                </p>
              </div>
            </div>

            {/* Confidence + cross-reference */}
            <div className="grid grid-cols-2 gap-4 py-3">
              <div>
                <p className="text-2xl font-bold tabular-nums text-foreground">
                  {(integrity.avg_confidence * 100).toFixed(0)}%
                </p>
                <p className="text-[11px] text-muted-foreground mt-0.5">avg. confidence</p>
                <p className="text-[10px] text-muted-foreground">{integrity.low_confidence_concepts} below 0.6</p>
              </div>
              <div>
                <p className="text-2xl font-bold tabular-nums text-foreground">{integrity.multi_doc_concepts}</p>
                <p className="text-[11px] text-muted-foreground mt-0.5">cross-referenced</p>
                <p className="text-[10px] text-muted-foreground">{integrity.multi_doc_pct.toFixed(0)}% of concepts</p>
              </div>
            </div>

            {integrity.unconnected_candidates > 0 && (
              <div className="border-t pt-3 text-[11px] text-muted-foreground flex items-start gap-2">
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                <span>
                  {integrity.unconnected_candidates} unconnected candidate{integrity.unconnected_candidates !== 1 ? 's' : ''} — concepts
                  from relationship-rich documents that are missing links. Adding more source documents may resolve this.
                </span>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Deliverable readiness ────────────────────────────────────────── */}
      <div className="border bg-white px-5 py-5">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider pb-3 border-b mb-1">
          Deliverable Readiness
        </h2>
        <ReadinessList
          client101={report.client_101_ready}
          client201={report.client_201_ready}
          execSummary={report.executive_summary_ready}
        />
      </div>

      {/* ── Ingestion summary ────────────────────────────────────────────── */}
      <div className="border bg-white px-5 py-5">
        <div className="flex items-baseline justify-between pb-3 border-b mb-1">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
            Ingestion Summary
          </h2>
          <div className="flex items-center gap-4 text-[11px] text-muted-foreground">
            <span>{report.complete_documents} complete</span>
            {report.failed_documents > 0 && (
              <span className="text-foreground font-medium">{report.failed_documents} failed</span>
            )}
            {report.pending_processing_documents > 0 && (
              <span>{report.pending_processing_documents} pending</span>
            )}
          </div>
        </div>
        {report.documents.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted-foreground">No documents ingested yet.</div>
        ) : (
          <>
            <div className="grid px-0 py-2 border-b text-[10px] font-semibold text-muted-foreground uppercase tracking-wider"
                 style={{ gridTemplateColumns: '1.5rem 1fr 7rem 8rem 3.5rem 1.5rem' }}>
              <span></span>
              <span>Document</span>
              <span className="text-right">Concepts</span>
              <span className="text-right">Relationships</span>
              <span className="text-right">Score</span>
              <span></span>
            </div>
            {report.documents
              .sort((a, b) => b.document_health_score - a.document_health_score)
              .map(doc => (
                <DocumentRow key={doc.document_id} doc={doc} />
              ))}
          </>
        )}
      </div>

    </main>
  );
}
