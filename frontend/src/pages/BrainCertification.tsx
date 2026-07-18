import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '@/api/client';
import type {
  RetrievalCertificationReport,
  KnowledgeGap,
  QuestionResult,
} from '@/types/api';
import {
  AlertCircle, CheckCircle2, ChevronDown, ChevronUp,
  Loader2, RefreshCw, ShieldCheck, Zap,
} from 'lucide-react';

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function scoreLabel(s: number): string {
  if (s >= 85) return 'Strong';
  if (s >= 70) return 'Good';
  if (s >= 55) return 'Moderate';
  return 'Insufficient';
}

function verdictLabel(v: string): string {
  if (v === 'CERTIFIED')     return 'CERTIFIED';
  if (v === 'PROVISIONAL')   return 'PROVISIONAL';
  if (v === 'NOT_CERTIFIED') return 'NOT CERTIFIED';
  if (v === 'RUNNING')       return 'RUNNING';
  return 'PENDING';
}

function verdictDescription(v: string): string {
  if (v === 'CERTIFIED')     return 'All retrieval thresholds pass. This workspace supports reliable consulting deliverables.';
  if (v === 'PROVISIONAL')   return 'Most thresholds pass. Deliverables are supported with some retrieval limitations.';
  if (v === 'NOT_CERTIFIED') return 'One or more retrieval thresholds fail. Review knowledge gaps before generating deliverables.';
  return 'No certification run completed for this workspace.';
}

// ─────────────────────────────────────────────────────────────────────────────
// MetricRow — single retrieval metric
// ─────────────────────────────────────────────────────────────────────────────

function MetricRow({
  label,
  score,
  passes,
  threshold,
  description,
}: {
  label: string;
  score: number;
  passes: boolean;
  threshold: number;
  description: string;
}) {
  return (
    <div className="py-3.5 border-b last:border-b-0">
      <div className="flex items-baseline justify-between mb-1.5">
        <div className="flex items-center gap-2">
          {passes
            ? <CheckCircle2 className="w-3.5 h-3.5 text-foreground flex-shrink-0" />
            : <AlertCircle className="w-3.5 h-3.5 text-foreground/40 flex-shrink-0" />
          }
          <span className="text-sm text-foreground">{label}</span>
        </div>
        <div className="flex items-baseline gap-2 flex-shrink-0">
          <span className="text-[11px] text-muted-foreground">threshold {threshold}%</span>
          <span className="text-xs text-muted-foreground">{scoreLabel(score)}</span>
          <span className="text-sm font-semibold tabular-nums text-foreground w-8 text-right">
            {score.toFixed(0)}
          </span>
        </div>
      </div>
      <div className="h-px w-full bg-foreground/[0.08] ml-5.5">
        <div
          className="h-full bg-foreground/50 transition-all"
          style={{ width: `${Math.min(100, score)}%` }}
        />
      </div>
      <p className="text-[11px] text-muted-foreground mt-1.5 ml-5.5 leading-relaxed pl-5">{description}</p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Benchmark question row
// ─────────────────────────────────────────────────────────────────────────────

function QuestionRow({ qr }: { qr: QuestionResult }) {
  const [open, setOpen] = useState(false);
  const recallPct    = Math.round(qr.recall    * 100);
  const precisionPct = Math.round(qr.precision * 100);

  return (
    <div className="border-b last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 px-5 py-3 text-left hover:bg-foreground/[0.02] transition-colors"
      >
        <span className="text-[10px] text-muted-foreground w-5 flex-shrink-0 tabular-nums text-right">
          {qr.question_id}
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-xs text-foreground truncate">{qr.question_text}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 capitalize">
            {qr.question_type.replace(/_/g, ' ')}
            {qr.has_evidence ? ' · evidenced' : ' · no evidence chain'}
          </p>
        </div>
        <div className="flex items-center gap-5 flex-shrink-0 text-[11px] text-muted-foreground">
          <span>Recall <span className="font-semibold text-foreground">{recallPct}%</span></span>
          <span>Precision <span className="font-semibold text-foreground">{precisionPct}%</span></span>
        </div>
        {open
          ? <ChevronUp className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
          : <ChevronDown className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
        }
      </button>
      {open && (
        <div className="px-12 pb-4 space-y-1.5 text-[11px] text-muted-foreground bg-foreground/[0.01]">
          <p>
            <span>Ground truth: </span>
            <span className="font-medium text-foreground">{qr.ground_truth_ids.length} concepts</span>
            {'  '}
            <span>Retrieved: </span>
            <span className="font-medium text-foreground">{qr.retrieved_ids.length} concepts</span>
          </p>
          {!qr.has_evidence && (
            <p className="flex items-center gap-1.5">
              <AlertCircle className="w-3 h-3 flex-shrink-0" />
              No retrieved concept had a source excerpt — evidence chain is broken.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Knowledge gap row
// ─────────────────────────────────────────────────────────────────────────────

const GAP_LABELS: Record<string, string> = {
  never_retrieved: 'Not retrieved',
  no_evidence:     'No evidence',
  orphaned:        'Isolated',
};

function GapRow({ gap }: { gap: KnowledgeGap }) {
  return (
    <div className="flex items-center gap-4 px-5 py-2.5 border-b last:border-b-0">
      <span className="text-[10px] text-muted-foreground border px-1.5 py-0.5 flex-shrink-0 w-20 text-center">
        {GAP_LABELS[gap.gap_type] ?? gap.gap_type}
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-xs text-foreground truncate">{gap.concept_name}</p>
        <p className="text-[10px] text-muted-foreground">{gap.concept_type}</p>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Pending / no-run state
// ─────────────────────────────────────────────────────────────────────────────

function PendingState({ onRun, running }: { onRun: () => void; running: boolean }) {
  return (
    <div className="border bg-white px-8 py-12 flex flex-col items-center gap-5 text-center">
      <ShieldCheck className="w-8 h-8 text-foreground/20" />
      <div>
        <p className="text-sm font-semibold text-foreground">No certification run completed</p>
        <p className="text-xs text-muted-foreground mt-1.5 max-w-sm leading-relaxed">
          Run the retrieval benchmark to measure recall, precision, coverage, consistency,
          and evidence fidelity. Results determine whether this workspace is certified
          to support consulting deliverables.
        </p>
      </div>
      <button
        type="button"
        onClick={onRun}
        disabled={running}
        className="flex items-center gap-2 px-5 py-2 bg-foreground text-background text-xs font-medium hover:opacity-90 transition-opacity disabled:opacity-40"
      >
        {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
        {running ? 'Running…' : 'Run Certification Benchmark'}
      </button>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main page
// ─────────────────────────────────────────────────────────────────────────────

export default function BrainCertification() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);

  const [report, setReport]   = useState<RetrievalCertificationReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError]     = useState('');
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const loadLatest = async () => {
    setLoading(true);
    setError('');
    try {
      const r = await api.certification.getReport(wsId);
      setReport(r);
      setLastRefreshed(new Date());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Load failed.');
    } finally {
      setLoading(false);
    }
  };

  const runCertification = async () => {
    setRunning(true);
    setError('');
    try {
      const r = await api.certification.run(wsId);
      setReport(r);
      setLastRefreshed(new Date());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Certification failed.');
    } finally {
      setRunning(false);
    }
  };

  useEffect(() => { loadLatest(); }, [wsId]);

  if (loading && !report) {
    return (
      <main className="max-w-4xl mx-auto px-8 py-12">
        <div className="py-20 flex flex-col items-center gap-3 text-muted-foreground">
          <Loader2 className="w-6 h-6 animate-spin" />
          <p className="text-sm">Loading certification…</p>
        </div>
      </main>
    );
  }

  return (
    <main className="max-w-4xl mx-auto px-8 py-10 space-y-10">

      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div className="pb-6 border-b flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Trust & Reliability</h1>
          <p className="text-sm text-muted-foreground mt-1.5 leading-relaxed max-w-xl">
            Retrieval accuracy, recall, precision, coverage, and evidence fidelity.
            Certification determines whether this workspace can reliably support consulting deliverables.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            type="button"
            onClick={loadLatest}
            disabled={loading || running}
            className="flex items-center gap-1.5 text-xs border px-3 py-1.5 hover:bg-muted transition-colors disabled:opacity-40"
          >
            {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            Refresh
          </button>
          <button
            type="button"
            onClick={runCertification}
            disabled={running || loading}
            className="flex items-center gap-1.5 text-xs bg-foreground text-background px-3 py-1.5 hover:opacity-90 transition-opacity disabled:opacity-40"
          >
            {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
            {running ? 'Running…' : 'Run Benchmark'}
          </button>
        </div>
      </div>

      {error && (
        <div className="callout callout-amber text-sm">{error}</div>
      )}

      {report && lastRefreshed && (
        <p className="text-[11px] text-muted-foreground -mt-8">
          Last run {new Date(report.generated_at).toLocaleString()} · Graph v{report.graph_version} ·{' '}
          {report.questions_generated} benchmark questions
        </p>
      )}

      {/* ── No run yet ──────────────────────────────────────────────────── */}
      {!report && !loading && (
        <PendingState onRun={runCertification} running={running} />
      )}

      {report && (
        <>
          {/* ── Certification verdict ───────────────────────────────────── */}
          <div className="border bg-white px-7 py-6">
            <div className="flex items-baseline justify-between mb-1">
              <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                Retrieval Certification
              </h2>
              <span className="text-[11px] text-muted-foreground">
                {report.questions_answered} of {report.questions_generated} questions answered
              </span>
            </div>
            <div className="flex items-end gap-5 mt-3">
              <div>
                <p className="text-6xl font-bold tabular-nums text-foreground leading-none">
                  {report.certification_score.toFixed(0)}
                </p>
                <p className="text-xs text-muted-foreground mt-1">out of 100</p>
              </div>
              <div className="flex-1 pb-2">
                <p className="text-base font-semibold text-foreground mb-1">
                  {verdictLabel(report.certification_status)}
                </p>
                <p className="text-xs text-muted-foreground leading-relaxed max-w-md">
                  {verdictDescription(report.certification_status)}
                </p>
                <div className="flex items-center gap-4 mt-2.5 text-[11px] text-muted-foreground">
                  <span>Retrieval accuracy <span className="font-semibold text-foreground">{report.retrieval_accuracy.toFixed(0)}%</span></span>
                  <span>Recall <span className="font-semibold text-foreground">{report.recall_score.toFixed(0)}%</span></span>
                  <span>Precision <span className="font-semibold text-foreground">{report.precision_score.toFixed(0)}%</span></span>
                </div>
              </div>
            </div>
            <div className="mt-5 h-1 w-full bg-foreground/[0.08]">
              <div
                className="h-full bg-foreground transition-all duration-500"
                style={{ width: `${Math.min(100, report.certification_score)}%` }}
              />
            </div>
          </div>

          {/* ── Two columns: retrieval metrics + coverage ────────────────── */}
          <div className="grid grid-cols-2 gap-8">

            {/* Retrieval metrics */}
            <div className="border bg-white px-5 py-5">
              <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1 pb-3 border-b">
                Retrieval Metrics
              </h2>
              <MetricRow
                label="Recall"
                score={report.recall_score}
                passes={report.recall_pass}
                threshold={70}
                description="Fraction of ground-truth concepts retrieved per question."
              />
              <MetricRow
                label="Precision"
                score={report.precision_score}
                passes={report.precision_pass}
                threshold={65}
                description="Fraction of retrieved concepts that are actually relevant."
              />
              <MetricRow
                label="Coverage"
                score={report.coverage_score}
                passes={report.coverage_pass}
                threshold={60}
                description="Fraction of all workspace concepts surfaced at least once."
              />
              <MetricRow
                label="Consistency"
                score={report.consistency_score}
                passes={report.consistency_pass}
                threshold={80}
                description="Stability of retrieval results across repeated identical queries."
              />
              <MetricRow
                label="Evidence Fidelity"
                score={report.evidence_fidelity}
                passes={report.evidence_pass}
                threshold={70}
                description="Fraction of retrieved results with traceable source evidence."
              />
            </div>

            {/* Coverage + threshold gates */}
            <div className="space-y-6">

              {/* Knowledge coverage */}
              <div className="border bg-white px-5 py-5">
                <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider pb-3 border-b mb-1">
                  Knowledge Coverage
                </h2>
                <div className="py-2 space-y-3 text-sm">
                  <div className="flex justify-between items-baseline">
                    <span className="text-muted-foreground text-[11px]">Concepts covered</span>
                    <span className="font-semibold text-foreground tabular-nums">
                      {report.concepts_covered} / {report.total_workspace_concepts}
                    </span>
                  </div>
                  <div className="flex justify-between items-baseline">
                    <span className="text-muted-foreground text-[11px]">Never retrieved</span>
                    <span className="font-semibold text-foreground tabular-nums">
                      {report.concepts_never_retrieved}
                    </span>
                  </div>
                  <div className="flex justify-between items-baseline">
                    <span className="text-muted-foreground text-[11px]">Knowledge gaps identified</span>
                    <span className="font-semibold text-foreground tabular-nums">
                      {report.knowledge_gaps.length}
                    </span>
                  </div>
                  <div className="flex justify-between items-baseline">
                    <span className="text-muted-foreground text-[11px]">Benchmark questions</span>
                    <span className="font-semibold text-foreground tabular-nums">
                      {report.questions_generated}
                    </span>
                  </div>
                </div>
              </div>

              {/* Threshold gates — certification criteria */}
              <div className="border bg-white px-5 py-5">
                <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider pb-3 border-b mb-1">
                  Certification Criteria
                </h2>
                <div className="space-y-0">
                  {[
                    { label: 'Recall',       threshold: '≥ 70%', pass: report.recall_pass,      score: report.recall_score      },
                    { label: 'Precision',    threshold: '≥ 65%', pass: report.precision_pass,   score: report.precision_score   },
                    { label: 'Coverage',     threshold: '≥ 60%', pass: report.coverage_pass,    score: report.coverage_score    },
                    { label: 'Consistency',  threshold: '≥ 80%', pass: report.consistency_pass, score: report.consistency_score },
                    { label: 'Evidence',     threshold: '≥ 70%', pass: report.evidence_pass,    score: report.evidence_fidelity },
                  ].map(({ label, threshold, pass, score }) => (
                    <div key={label} className="flex items-center gap-3 py-2.5 border-b last:border-b-0">
                      {pass
                        ? <CheckCircle2 className="w-3.5 h-3.5 text-foreground flex-shrink-0" />
                        : <AlertCircle className="w-3.5 h-3.5 text-foreground/30 flex-shrink-0" />
                      }
                      <span className="flex-1 text-xs text-foreground">{label}</span>
                      <span className="text-[10px] text-muted-foreground">{threshold}</span>
                      <span className="text-xs font-semibold tabular-nums text-foreground w-8 text-right">
                        {score.toFixed(0)}%
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>

          {/* ── Knowledge gaps ───────────────────────────────────────────── */}
          {report.knowledge_gaps.length > 0 && (
            <div className="border bg-white">
              <div className="px-5 py-4 border-b flex items-baseline justify-between">
                <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                  Knowledge Gaps
                </h2>
                <span className="text-[11px] text-muted-foreground">
                  {report.knowledge_gaps.length} concept{report.knowledge_gaps.length !== 1 ? 's' : ''} not reached by retrieval
                </span>
              </div>
              <div className="grid px-5 py-2 border-b text-[10px] font-semibold text-muted-foreground uppercase tracking-wider"
                   style={{ gridTemplateColumns: '6rem 1fr' }}>
                <span>Status</span>
                <span>Concept</span>
              </div>
              <div className="max-h-64 overflow-y-auto">
                {report.knowledge_gaps.map(g => (
                  <GapRow key={g.concept_id} gap={g} />
                ))}
              </div>
              <div className="px-5 py-3 border-t">
                <p className="text-[11px] text-muted-foreground leading-relaxed">
                  Gaps indicate concepts present in the knowledge graph but not surfaced by retrieval.
                  Consider reviewing document coverage or re-ingesting related source documents.
                </p>
              </div>
            </div>
          )}

          {/* ── Benchmark questions ──────────────────────────────────────── */}
          <div className="border bg-white">
            <div className="px-5 py-4 border-b flex items-baseline justify-between">
              <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                Benchmark Questions
              </h2>
              <span className="text-[11px] text-muted-foreground">
                {report.question_results.length} question{report.question_results.length !== 1 ? 's' : ''} · click to expand
              </span>
            </div>
            {report.question_results.length === 0 ? (
              <div className="py-8 text-center text-sm text-muted-foreground">
                No questions were generated for this run.
              </div>
            ) : (
              <>
                <div className="grid px-5 py-2 border-b text-[10px] font-semibold text-muted-foreground uppercase tracking-wider"
                     style={{ gridTemplateColumns: '1.5rem 1fr 5rem 6rem 1.5rem' }}>
                  <span>#</span>
                  <span>Question</span>
                  <span className="text-right">Recall</span>
                  <span className="text-right">Precision</span>
                  <span></span>
                </div>
                {report.question_results.map(qr => (
                  <QuestionRow key={qr.question_id} qr={qr} />
                ))}
              </>
            )}
          </div>
        </>
      )}

    </main>
  );
}
