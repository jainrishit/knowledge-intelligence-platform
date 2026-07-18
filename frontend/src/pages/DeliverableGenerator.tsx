import { useEffect, useRef, useState, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '@/api/client';
import type {
  Deliverable, DeliverableType, PlanSlide, PresentationPlan, Workspace,
} from '@/types/api';
import {
  AlertCircle, CheckCircle2, ChevronDown, ChevronUp, Clock,
  FileDown, GitBranch, Loader2, Network, Presentation,
  RefreshCw, RotateCcw, Send, Trash2, TrendingUp, Cpu,
} from 'lucide-react';

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

type Tab = 'generate' | 'list';
type Stage = 'select' | 'planning' | 'review' | 'generating' | 'done';

interface TypeConfig {
  id: DeliverableType;
  label: string;
  headline: string;
  hasFocusArea: false | string;
}

const TYPES: TypeConfig[] = [
  {
    id: 'client_101',
    label: 'Client 101',
    headline: 'Full client briefing for a new engagement team',
    hasFocusArea: false,
  },
  {
    id: 'client_201',
    label: 'Client 201',
    headline: 'Deep consulting analysis for experienced engagement teams',
    hasFocusArea: false,
  },
  {
    id: 'executive_summary',
    label: 'Executive Summary',
    headline: 'Leadership briefing on a focused topic',
    hasFocusArea: 'Area of focus (e.g. ISO 20022, Cross-Border Payments)',
  },
];

const TYPE_LABEL: Record<string, string> = {
  client_101: 'Client 101',
  client_201: 'Client 201',
  executive_summary: 'Executive Summary',
};

// Layout descriptions for the plan review card
const LAYOUT_LABELS: Record<string, string> = {
  cover: 'Cover',
  title_content: 'Bullets',
  two_column: '2-Column',
  two_col_dividers: '2-Col Dividers',
  four_column: '4-Column',
  four_column_headlines: '4-Col Headlines',
  four_boxes_wide: '4 Boxes (wide)',
  four_boxes_stacked: '4 Boxes (stacked)',
  six_boxes: '6 Boxes',
  data_2_callouts: 'Data Callouts',
  callout_stat: 'Stat Callout',
  large_text: 'Large Text',
  section_divider: 'Section Divider',
  agenda: 'Agenda',
  sources: 'Sources',
  end_slide: 'End Slide',
};

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function slideContentPreview(slide: PlanSlide): string {
  if (slide.bullets?.length) return slide.bullets[0];
  if (slide.boxes?.length) return slide.boxes[0];
  if (slide.stats?.length) return `${slide.stats[0].label}: ${slide.stats[0].body}`;
  if (slide.columns?.length) return slide.columns[0]?.[0] ?? '';
  return '';
}

// ─────────────────────────────────────────────────────────────────────────────
// WorkspaceKnowledgeSummary
// ─────────────────────────────────────────────────────────────────────────────

function WorkspaceKnowledgeSummary({ ws }: { ws: Workspace | null }) {
  if (!ws) return null;
  const hasKnowledge = ws.concept_count > 0;

  if (!hasKnowledge) {
    return (
      <div className="mb-6 flex items-start gap-3 px-4 py-3 border bg-amber-50 border-amber-200 text-xs">
        <AlertCircle className="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
        <div>
          <p className="font-semibold text-amber-800">No knowledge compiled yet</p>
          <p className="text-amber-700 mt-0.5">
            Upload and process documents in the Documents tab first.
            Client materials are generated exclusively from compiled knowledge.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="mb-6 border bg-white">
      <div className="px-4 py-2.5 border-b flex items-center gap-2">
        <Cpu className="w-3.5 h-3.5 text-green-500" />
        <span className="text-xs font-semibold text-green-700">Knowledge graph active</span>
        <span className="text-[10px] text-muted-foreground ml-1">
          v{ws.graph_version} · {ws.document_count} source {ws.document_count === 1 ? 'document' : 'documents'}
        </span>
        <span className="ml-auto text-[10px] text-muted-foreground">Presentation will draw from this compiled knowledge</span>
      </div>
      <div className="grid grid-cols-3 divide-x text-center">
        <div className="px-4 py-3">
          <p className="text-lg font-bold text-foreground tabular-nums">{ws.concept_count.toLocaleString()}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <Network className="w-3 h-3" /> concepts
          </p>
        </div>
        <div className="px-4 py-3">
          <p className="text-lg font-bold text-foreground tabular-nums">{ws.relationship_count}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <GitBranch className="w-3 h-3" /> relationships
          </p>
        </div>
        <div className="px-4 py-3">
          <p className="text-lg font-bold text-foreground tabular-nums">{ws.pattern_count}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <TrendingUp className="w-3 h-3" /> patterns
          </p>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SlideCard — single slide in the plan review
// ─────────────────────────────────────────────────────────────────────────────

function SlideCard({
  slide,
  index,
  total,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  slide: PlanSlide;
  index: number;
  total: number;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const isStructural = ['section_divider', 'cover', 'sources', 'end_slide', 'agenda'].includes(slide.layout);
  const preview = slideContentPreview(slide);

  return (
    <div className={`border bg-white ${isStructural ? 'opacity-75' : ''}`}>
      <div className="flex items-start gap-3 px-4 py-3">
        {/* Slide number badge */}
        <div className="flex-shrink-0 w-7 h-7 flex items-center justify-center bg-foreground/[0.06] text-[11px] font-bold text-foreground">
          {slide.slide_number}
        </div>

        {/* Main content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-start gap-2">
            <p className="text-sm font-semibold text-foreground leading-snug flex-1">{slide.title}</p>
            <span className="flex-shrink-0 text-[10px] px-1.5 py-0.5 bg-foreground/[0.05] text-muted-foreground border">
              {LAYOUT_LABELS[slide.layout] ?? slide.layout}
            </span>
          </div>
          {slide.purpose && (
            <p className="text-[11px] text-muted-foreground mt-0.5 italic">{slide.purpose}</p>
          )}
          {!expanded && preview && (
            <p className="text-[11px] text-muted-foreground mt-1 truncate">{preview}</p>
          )}
          {expanded && (
            <div className="mt-2 space-y-2.5">
              {slide.section && (
                <p className="text-[11px] text-muted-foreground">
                  <span className="font-medium">Section:</span> {slide.section}
                </p>
              )}
              {slide.bullets?.length > 0 && (
                <ul className="space-y-0.5">
                  {slide.bullets.map((b, i) => (
                    <li key={i} className="text-[11px] text-foreground flex gap-1.5">
                      <span className="text-muted-foreground flex-shrink-0">·</span>
                      <span>{b}</span>
                    </li>
                  ))}
                </ul>
              )}
              {slide.boxes?.length ? (
                <div className="grid grid-cols-2 gap-1.5">
                  {slide.boxes.map((b, i) => (
                    <div key={i} className="text-[11px] px-2 py-1.5 bg-foreground/[0.03] border">
                      {b}
                    </div>
                  ))}
                </div>
              ) : null}
              {slide.stats?.length ? (
                <div className="flex gap-3">
                  {slide.stats.map((s, i) => (
                    <div key={i} className="flex-1 border px-2 py-1.5">
                      <p className="text-xs font-bold text-foreground">{s.label}</p>
                      <p className="text-[11px] text-muted-foreground">{s.body}</p>
                    </div>
                  ))}
                </div>
              ) : null}

              {/* ── Key insights + Knowledge sources used by this slide ───── */}
              {(slide.key_insights?.length > 0 || slide.graph_concepts?.length > 0
                || slide.relationships_used?.length > 0 || slide.patterns_used?.length > 0
                || slide.evidence?.length > 0) && (
                <div className="border-t pt-2.5 space-y-2">
                  {slide.key_insights?.length > 0 && (
                    <div className="bg-foreground/[0.03] border px-3 py-2.5 space-y-1">
                      <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                        Key insights
                      </p>
                      <ul className="space-y-1">
                        {slide.key_insights.map((insight, i) => (
                          <li key={i} className="text-[11px] text-foreground flex gap-1.5">
                            <span className="text-muted-foreground flex-shrink-0 font-bold">→</span>
                            <span>{insight}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
                    Knowledge sources
                  </p>
                  {slide.graph_concepts?.length > 0 && (
                    <div>
                      <p className="text-[10px] text-muted-foreground mb-1">Key topics</p>
                      <div className="flex flex-wrap gap-1">
                        {slide.graph_concepts.map((c, i) => (
                          <span key={i} className="text-[10px] px-1.5 py-0.5 border border-foreground/15 text-foreground/70 bg-foreground/[0.02]">
                            {c}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {slide.relationships_used?.length > 0 && (
                    <div>
                      <p className="text-[10px] text-muted-foreground mb-1">Referenced relationships</p>
                      <ul className="space-y-0.5">
                        {slide.relationships_used.map((r, i) => (
                          <li key={i} className="text-[10px] text-muted-foreground">→ {r}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {slide.patterns_used?.length > 0 && (
                    <div>
                      <p className="text-[10px] text-muted-foreground mb-1">Consulting patterns</p>
                      <div className="flex flex-wrap gap-1">
                        {slide.patterns_used.map((p, i) => (
                          <span key={i} className="text-[10px] px-1.5 py-0.5 border border-foreground/15 text-foreground/70 bg-foreground/[0.02]">
                            {p}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {slide.evidence?.length > 0 && (
                    <div>
                      <p className="text-[10px] text-muted-foreground mb-1">Supporting evidence</p>
                      <div className="flex flex-wrap gap-1">
                        {slide.evidence.map((e, i) => (
                          <span key={i} className="text-[10px] px-1.5 py-0.5 border border-foreground/15 text-foreground/70 bg-foreground/[0.02]">
                            {e}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {slide.visual_recommendation && (
                <p className="text-[11px] text-muted-foreground italic">
                  <span className="font-medium not-italic">Visual: </span>
                  {slide.visual_recommendation}
                </p>
              )}
              {slide.notes && (
                <p className="text-[11px] text-muted-foreground">
                  <span className="font-medium">Speaker notes: </span>
                  {slide.notes}
                </p>
              )}
            </div>
          )}
        </div>

        {/* Controls */}
        <div className="flex flex-col items-center gap-1 flex-shrink-0">
          <button
            type="button"
            title="Move up"
            disabled={index === 0}
            onClick={onMoveUp}
            className="p-1 hover:bg-muted disabled:opacity-20 transition-colors"
          >
            <ChevronUp className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            title="Move down"
            disabled={index === total - 1}
            onClick={onMoveDown}
            className="p-1 hover:bg-muted disabled:opacity-20 transition-colors"
          >
            <ChevronDown className="w-3.5 h-3.5" />
          </button>
        </div>
        <button
          type="button"
          title="Remove slide"
          onClick={onRemove}
          className="flex-shrink-0 p-1 text-muted-foreground hover:text-foreground transition-colors"
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
        <button
          type="button"
          onClick={() => setExpanded(e => !e)}
          className="flex-shrink-0 p-1 text-muted-foreground hover:text-foreground transition-colors"
          title={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
        </button>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// PlanIntelligenceSummary — compact bar showing graph coverage across the plan
// ─────────────────────────────────────────────────────────────────────────────

function PlanIntelligenceSummary({ slides }: { slides: PlanSlide[] }) {
  const contentSlides = slides.filter(
    s => !['section_divider', 'cover', 'sources', 'end_slide', 'agenda'].includes(s.layout)
  );

  // Unique concepts used across all content slides
  const allConcepts = new Set<string>();
  const allEvidence = new Set<string>();
  const allRelationships = new Set<string>();
  let slidesWithInsights = 0;

  for (const s of contentSlides) {
    s.graph_concepts?.forEach(c => allConcepts.add(c));
    s.evidence?.forEach(e => allEvidence.add(e));
    s.relationships_used?.forEach(r => allRelationships.add(r));
    if (s.key_insights?.length > 0) slidesWithInsights++;
  }

  if (contentSlides.length === 0) return null;

  return (
    <div className="border bg-white px-4 py-3 space-y-2.5">
      <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
        Plan intelligence coverage
      </p>
      <div className="grid grid-cols-4 gap-3 text-center">
        <div className="border px-2 py-2">
          <p className="text-base font-bold text-foreground tabular-nums">{allConcepts.size}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5">concepts used</p>
        </div>
        <div className="border px-2 py-2">
          <p className="text-base font-bold text-foreground tabular-nums">{allRelationships.size}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5">relationships cited</p>
        </div>
        <div className="border px-2 py-2">
          <p className="text-base font-bold text-foreground tabular-nums">{allEvidence.size}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5">source docs</p>
        </div>
        <div className="border px-2 py-2">
          <p className="text-base font-bold text-foreground tabular-nums">
            {slidesWithInsights}/{contentSlides.length}
          </p>
          <p className="text-[10px] text-muted-foreground mt-0.5">with insights</p>
        </div>
      </div>
      {allEvidence.size > 0 && (
        <div className="flex flex-wrap gap-1 pt-1 border-t">
          {[...allEvidence].map((e, i) => (
            <span key={i} className="text-[10px] px-1.5 py-0.5 bg-foreground/[0.04] border text-muted-foreground">
              {e}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// PlanReview — step 2 of the workflow
// ─────────────────────────────────────────────────────────────────────────────

function PlanReview({
  plan,
  onPlanUpdated,
  onApprove,
  onStartOver,
  approving,
}: {
  plan: PresentationPlan;
  onPlanUpdated: (p: PresentationPlan) => void;
  onApprove: () => void;
  onStartOver: () => void;
  approving: boolean;
}) {
  const [slides, setSlides] = useState<PlanSlide[]>(plan.slides);
  const [instruction, setInstruction] = useState('');
  const [revising, setRevising] = useState(false);
  const [revisionError, setRevisionError] = useState('');
  // Track whether local slide edits are ahead of the server-side blueprint.
  const [slidesDirty, setSlidesDirty] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Sync when plan changes from outside (after a revision — server is now the source of truth)
  useEffect(() => {
    setSlides(plan.slides);
    setSlidesDirty(false);
  }, [plan.slides]);

  const contentSlides = slides.filter(
    s => !['section_divider', 'cover', 'sources', 'end_slide', 'agenda'].includes(s.layout)
  );
  const groundingPct = contentSlides.length > 0
    ? Math.round(100 * contentSlides.filter(
        s => (s.graph_concepts?.length > 0) || (s.evidence?.length > 0)
      ).length / contentSlides.length)
    : 0;

  // ── Local slide manipulation (reorder / remove without LLM round-trip) ───
  const moveSlide = (index: number, direction: -1 | 1) => {
    const next = [...slides];
    const target = index + direction;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    // Renumber
    setSlides(next.map((s, i) => ({ ...s, slide_number: i + 1 })));
    setSlidesDirty(true);
  };

  const removeSlide = (index: number) => {
    const next = slides.filter((_, i) => i !== index);
    setSlides(next.map((s, i) => ({ ...s, slide_number: i + 1 })));
    setSlidesDirty(true);
  };

  /**
   * Flush any pending local slide edits to the backend.
   * Returns the latest plan (from server) or null on error.
   * No-ops if slides are already in sync.
   */
  const flushSlideEdits = useCallback(async (): Promise<PresentationPlan | null> => {
    if (!slidesDirty) return null;
    try {
      const updated = await api.plans.updateSlides(plan.id, slides);
      onPlanUpdated(updated);
      setSlidesDirty(false);
      return updated;
    } catch {
      // Non-fatal — proceed anyway; backend will use its stored blueprint
      return null;
    }
  }, [plan.id, slides, slidesDirty, onPlanUpdated]);

  // ── Submit revision instruction to Claude ────────────────────────────────
  const submitRevision = async () => {
    if (!instruction.trim()) return;
    setRevising(true);
    setRevisionError('');
    try {
      // Persist any local reorder/remove edits BEFORE asking Claude to revise,
      // so the revision is applied on top of the user's current slide state.
      await flushSlideEdits();
      const updated = await api.plans.revise(plan.id, instruction.trim());
      onPlanUpdated(updated);
      setInstruction('');
    } catch (err: unknown) {
      setRevisionError(err instanceof Error ? err.message : 'Revision failed.');
    } finally {
      setRevising(false);
    }
  };

  return (
    <div className="space-y-6">

      {/* Plan header */}
      <div className="border bg-white px-5 py-4 space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1">
              Proposed deck · {TYPE_LABEL[plan.deliverable_type]}
              {plan.focus_area ? ` · ${plan.focus_area}` : ''}
            </p>
            <h2 className="text-base font-bold text-foreground leading-snug">
              {plan.deck_title || 'Untitled Deck'}
            </h2>
          </div>
          <div className="flex gap-2 flex-shrink-0 flex-wrap justify-end">
            <span className="text-[11px] px-2 py-1 bg-foreground/[0.05] border text-muted-foreground">
              {slides.length} slides
            </span>
            <span className="text-[11px] px-2 py-1 bg-foreground/[0.05] border text-muted-foreground">
              {contentSlides.length} content
            </span>
            <span className="text-[11px] px-2 py-1 bg-foreground/[0.05] border text-muted-foreground">
              {groundingPct}% sourced
            </span>
          </div>
        </div>

        {plan.storyline_summary && (
          <p className="text-xs text-muted-foreground border-t pt-3 leading-relaxed">
            {plan.storyline_summary}
          </p>
        )}

        {plan.governing_messages?.length > 0 && (
          <div className="border-t pt-3">
            <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
              Governing messages
            </p>
            <ul className="space-y-1">
              {plan.governing_messages.map((m, i) => (
                <li key={i} className="text-xs text-foreground flex gap-2">
                  <span className="text-muted-foreground">{i + 1}.</span>
                  <span>{m}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Intelligence coverage summary */}
      <PlanIntelligenceSummary slides={slides} />

      {/* Slide list */}
      <div>
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">
          Proposed slides — reorder, remove, or send a revision instruction below
        </p>
        <div className="space-y-1.5">
          {slides.map((slide, i) => (
            <SlideCard
              key={`${slide.slide_number}-${slide.title}`}
              slide={slide}
              index={i}
              total={slides.length}
              onMoveUp={() => moveSlide(i, -1)}
              onMoveDown={() => moveSlide(i, 1)}
              onRemove={() => removeSlide(i)}
            />
          ))}
        </div>
      </div>

      {/* Revision instruction box */}
      <div className="border bg-white p-5 space-y-3">
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          Request a revision
        </p>
        <p className="text-[11px] text-muted-foreground leading-relaxed">
          Describe the changes you want. Claude will apply your instruction and return an updated plan.
        </p>
        {/* Quick-fill suggestion chips — click to append to (or set) textarea */}
        <div className="flex flex-wrap gap-1.5">
          {[
            'Add more content on this topic.',
            'Expand the architecture section.',
            'Remove stakeholder slides.',
            'Add a dedicated technology landscape section.',
            'Reduce the number of slides to the most essential.',
            'Strengthen the recommendations section.',
            'Add a slide on implementation risks.',
            'Add a slide comparing current state vs target state.',
          ].map(suggestion => (
            <button
              key={suggestion}
              type="button"
              disabled={revising || approving}
              onClick={() => {
                setInstruction(prev =>
                  prev.trim() ? `${prev.trim()} ${suggestion}` : suggestion
                );
                textareaRef.current?.focus();
              }}
              className="text-[11px] px-2 py-1 border border-foreground/15 text-muted-foreground hover:border-foreground/40 hover:text-foreground hover:bg-muted/30 transition-colors disabled:opacity-40"
            >
              {suggestion}
            </button>
          ))}
        </div>
        <textarea
          ref={textareaRef}
          className="w-full border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white resize-none"
          rows={3}
          placeholder="Describe what you'd like to change…"
          value={instruction}
          onChange={e => setInstruction(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submitRevision();
          }}
          disabled={revising || approving}
        />
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={submitRevision}
            disabled={revising || approving || !instruction.trim()}
            className="flex items-center gap-1.5 px-4 py-2 border text-sm font-medium disabled:opacity-40 hover:bg-muted transition-colors"
          >
            {revising
              ? <><Loader2 className="w-4 h-4 animate-spin" /> Revising…</>
              : <><Send className="w-4 h-4" /> Apply revision</>
            }
          </button>
          {plan.revision_history?.length > 0 && (
            <span className="text-[11px] text-muted-foreground flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {plan.revision_history.length} revision{plan.revision_history.length > 1 ? 's' : ''} applied
            </span>
          )}
        </div>
        {revisionError && (
          <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 px-3 py-2">
            {revisionError}
          </p>
        )}
      </div>

      {/* Revision history */}
      {plan.revision_history?.length > 0 && (
        <div className="border bg-white p-5 space-y-3">
          <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
            Revision history
          </p>
          <div className="space-y-2">
            {plan.revision_history.map((r, i) => (
              <div key={i} className="border-l-2 border-foreground/20 pl-3 py-0.5">
                <p className="text-xs font-medium text-foreground">{r.instruction}</p>
                {r.changes?.length > 0 && (
                  <ul className="mt-1 space-y-0.5">
                    {r.changes.slice(0, 3).map((c, j) => (
                      <li key={j} className="text-[11px] text-muted-foreground">· {c}</li>
                    ))}
                    {r.changes.length > 3 && (
                      <li className="text-[11px] text-muted-foreground">
                        · …and {r.changes.length - 3} more
                      </li>
                    )}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Action bar */}
      <div className="border-t pt-5 flex items-center justify-between">
        <button
          type="button"
          onClick={onStartOver}
          disabled={revising || approving}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors disabled:opacity-40"
        >
          <RotateCcw className="w-4 h-4" /> Start over
        </button>
        <button
          type="button"
          onClick={async () => {
            // Flush any pending local edits before handing off to parent approval handler.
            await flushSlideEdits();
            onApprove();
          }}
          disabled={
            revising || approving || slides.length === 0 ||
            plan.status === 'approved'
          }
          title={plan.status === 'approved' ? 'This plan has already been approved' : undefined}
          className="flex items-center gap-2 px-6 py-2.5 bg-foreground text-background text-sm font-medium disabled:opacity-40 hover:opacity-80 transition-opacity"
        >
          {approving
            ? <><Loader2 className="w-4 h-4 animate-spin" /> Generating PPTX…</>
            : plan.status === 'approved'
              ? <><CheckCircle2 className="w-4 h-4" /> Already generated</>
              : <><CheckCircle2 className="w-4 h-4" /> Approve & generate PPTX</>
          }
        </button>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Saved Materials row
// ─────────────────────────────────────────────────────────────────────────────

function DeliverableRow({
  d,
  onDeleted,
}: {
  d: Deliverable;
  onDeleted: (id: number) => void;
}) {
  const [reExporting, setReExporting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [rowError, setRowError] = useState('');

  const handleReExport = async () => {
    setReExporting(true);
    setRowError('');
    try {
      const { blob, filename } = await api.deliverables.reExport(d.id);
      triggerDownload(blob, filename);
    } catch (err: unknown) {
      setRowError(err instanceof Error ? err.message : 'Export failed.');
    } finally {
      setReExporting(false);
    }
  };

  const handleDelete = async () => {
    setDeleting(true);
    setRowError('');
    try {
      await api.deliverables.delete(d.id);
      onDeleted(d.id);
    } catch (err: unknown) {
      setRowError(err instanceof Error ? err.message : 'Delete failed.');
      setDeleting(false);
    }
  };

  return (
    <div className="bg-white px-5 py-4 space-y-2">
      <div className="flex items-center gap-4">
        <div className="flex-shrink-0">
          <span className="type-pill type-General">
            {TYPE_LABEL[d.type] ?? d.type.replace(/_/g, ' ')}
          </span>
        </div>
        <div className="flex-1 min-w-0">
          <p className="font-medium text-sm text-foreground truncate">{d.title}</p>
          <p className="text-xs text-muted-foreground mt-0.5">
            {new Date(d.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
            {d.source_document_ids.length > 0 && ` · ${d.source_document_ids.length} source${d.source_document_ids.length > 1 ? 's' : ''}`}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            onClick={handleReExport}
            disabled={reExporting}
            className="flex items-center gap-1 text-xs border px-2.5 py-1.5 hover:bg-muted transition-colors disabled:opacity-40"
          >
            {reExporting
              ? <><Loader2 className="w-3 h-3 animate-spin" /> Generating…</>
              : <><FileDown className="w-3 h-3" /> Re-export .pptx</>
            }
          </button>
          <button
            onClick={handleDelete}
            disabled={deleting}
            className="flex items-center gap-1 text-xs border px-2.5 py-1.5 hover:bg-muted transition-colors disabled:opacity-40"
          >
            {deleting
              ? <Loader2 className="w-3 h-3 animate-spin" />
              : <><Trash2 className="w-3 h-3" /> Delete</>
            }
          </button>
        </div>
      </div>
      {rowError && (
        <p className="text-xs text-muted-foreground pl-1">{rowError}</p>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Stage progress indicator
// ─────────────────────────────────────────────────────────────────────────────

function StageIndicator({ stage }: { stage: Stage }) {
  const displaySteps = [
    { id: 'select',  label: '1. Select type' },
    { id: 'review',  label: '2. Review & revise plan' },
    { id: 'done',    label: '3. Approve & generate' },
  ] as const;

  const stageOrder: Record<string, number> = {
    select: 0, planning: 0, review: 1, generating: 2, done: 2,
  };
  const currentOrder = stageOrder[stage] ?? 0;

  return (
    <div className="flex items-center gap-0 mb-8">
      {displaySteps.map((s, i) => {
        const sOrder = stageOrder[s.id] ?? 0;
        const isActive = sOrder === currentOrder;
        const isDone = sOrder < currentOrder;
        return (
          <div key={s.id} className="flex items-center">
            <div className={`flex items-center gap-1.5 px-3 py-1.5 text-xs border transition-colors ${
              isActive
                ? 'bg-foreground text-background border-foreground font-semibold'
                : isDone
                  ? 'bg-foreground/[0.06] text-foreground border-foreground/30'
                  : 'text-muted-foreground border-border'
            }`}>
              {isDone && <CheckCircle2 className="w-3 h-3" />}
              {s.label}
            </div>
            {i < displaySteps.length - 1 && (
              <div className="w-8 h-px bg-border" />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main page
// ─────────────────────────────────────────────────────────────────────────────

export default function DeliverableGenerator() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);

  // ── Workspace data ────────────────────────────────────────────────────────
  const [workspace, setWorkspace] = useState<Workspace | null>(null);

  // ── Navigation ────────────────────────────────────────────────────────────
  const [tab, setTab] = useState<Tab>('generate');

  // ── Generate tab state ────────────────────────────────────────────────────
  const [stage, setStage] = useState<Stage>('select');

  // Step 1: selection
  const [delivType, setDelivType] = useState<DeliverableType>('client_101');
  const [focusArea, setFocusArea] = useState('');

  // Step 2: plan
  const [plan, setPlan] = useState<PresentationPlan | null>(null);
  const [approving, setApproving] = useState(false);

  // Step 3: done
  const [downloadReady, setDownloadReady] = useState<{
    url: string; filename: string; title: string; sourceCount: number;
  } | null>(null);

  // Shared error / loading
  const [error, setError] = useState('');
  // Elapsed-time counter for long-running loading stages
  const [elapsed, setElapsed] = useState(0);
  const elapsedRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const startTimer = () => {
    setElapsed(0);
    elapsedRef.current = setInterval(() => setElapsed(s => s + 1), 1000);
  };
  const stopTimer = () => {
    if (elapsedRef.current) { clearInterval(elapsedRef.current); elapsedRef.current = null; }
  };
  useEffect(() => stopTimer, []); // cleanup on unmount

  // ── Saved materials tab ───────────────────────────────────────────────────
  const [deliverables, setDeliverables] = useState<Deliverable[]>([]);
  const [listLoading, setListLoading] = useState(false);

  const selectedType = TYPES.find(t => t.id === delivType) ?? TYPES[0];

  // ── Effects ───────────────────────────────────────────────────────────────

  useEffect(() => {
    api.workspaces.get(wsId).then(setWorkspace).catch(() => null);
  }, [wsId]);

  useEffect(() => {
    if (tab === 'list') {
      setListLoading(true);
      api.deliverables.list(wsId).then(setDeliverables).finally(() => setListLoading(false));
    }
  }, [tab]);

  useEffect(() => {
    return () => { if (downloadReady?.url) URL.revokeObjectURL(downloadReady.url); };
  }, [downloadReady]);

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleGeneratePlan = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setStage('planning');
    startTimer();
    try {
      const newPlan = await api.plans.create(
        wsId,
        delivType,
        delivType === 'executive_summary' && focusArea.trim() ? focusArea.trim() : undefined,
      );
      setPlan(newPlan);
      setStage('review');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Plan generation failed.');
      setStage('select');
    } finally {
      stopTimer();
    }
  };

  const handlePlanUpdated = (updated: PresentationPlan) => {
    setPlan(updated);
  };

  const handleApprove = async () => {
    if (!plan) return;
    setApproving(true);
    setError('');
    setStage('generating');
    startTimer();
    try {
      const result = await api.plans.generate(plan.id);
      if (downloadReady?.url) URL.revokeObjectURL(downloadReady.url);
      const url = URL.createObjectURL(result.blob);
      setDownloadReady({ url, filename: result.filename, title: result.title, sourceCount: result.sourceCount });
      setStage('done');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'PPTX generation failed.');
      setStage('review');
    } finally {
      setApproving(false);
      stopTimer();
    }
  };

  const handleStartOver = () => {
    setPlan(null);
    setDownloadReady(null);
    setError('');
    setStage('select');
  };

  // ─────────────────────────────────────────────────────────────────────────
  // Render
  // ─────────────────────────────────────────────────────────────────────────

  return (
    <main className="max-w-4xl mx-auto px-8 py-10">

      <div className="mb-7 pb-6 border-b">
        <h1 className="text-2xl font-semibold text-foreground">Client Material Generator</h1>
        <p className="text-sm text-muted-foreground mt-1.5 leading-relaxed">
          Plan → review → approve → generate. Claude analyses the knowledge graph and
          proposes a slide-by-slide plan grounded in concepts, relationships, and evidence.
          Review, revise as needed, then approve to render the final PowerPoint.
        </p>
      </div>

      {/* Tab selector */}
      <div className="seg-group mb-8">
        <button
          className={`seg-btn${tab === 'generate' ? ' active' : ''}`}
          onClick={() => setTab('generate')}
        >
          Generate new
        </button>
        <button
          className={`seg-btn${tab === 'list' ? ' active' : ''}`}
          onClick={() => setTab('list')}
        >
          Saved materials
        </button>
      </div>

      {/* Knowledge summary — always visible */}
      <WorkspaceKnowledgeSummary ws={workspace} />

      {/* ── Generate tab ──────────────────────────────────────────────────── */}
      {tab === 'generate' && (
        <div>
          {/* Stage progress */}
          {stage !== 'select' && <StageIndicator stage={stage} />}

          {/* ── Stage: select ────────────────────────────────────────────── */}
          {stage === 'select' && (
            <form onSubmit={handleGeneratePlan} className="border bg-white p-6 space-y-6">
              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">
                  Material type
                </label>
                <div className="grid grid-cols-3 gap-3">
                  {TYPES.map(t => (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => { setDelivType(t.id); setFocusArea(''); }}
                      className={`text-left p-3.5 border transition-colors ${
                        delivType === t.id
                          ? 'border-foreground bg-foreground/[0.03]'
                          : 'border-border hover:border-muted-foreground/40 hover:bg-muted/20'
                      }`}
                    >
                      <p className={`text-xs font-semibold mb-0.5 ${delivType === t.id ? 'text-foreground' : 'text-muted-foreground'}`}>
                        {t.label}
                      </p>
                      <p className="text-[11px] text-muted-foreground leading-snug">{t.headline}</p>
                    </button>
                  ))}
                </div>
              </div>

              {selectedType.hasFocusArea && (
                <div>
                  <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                    Area of focus
                    <span className="normal-case font-normal ml-1 text-muted-foreground">(optional)</span>
                  </label>
                  <input
                    className="w-full border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white"
                    placeholder={typeof selectedType.hasFocusArea === 'string' ? selectedType.hasFocusArea : ''}
                    value={focusArea}
                    onChange={e => setFocusArea(e.target.value)}
                  />
                  <p className="text-[11px] text-muted-foreground mt-1">
                    The graph will be traversed from this topic outward. Leave blank to cover the most significant theme.
                  </p>
                </div>
              )}

              {error && (
                <div className="callout callout-amber text-sm">
                  <strong>Error:</strong> {error}
                </div>
              )}

              <div className="flex justify-end pt-1 border-t">
                <button
                  type="submit"
                  disabled={(workspace?.concept_count ?? 0) === 0}
                  className="flex items-center gap-2 px-5 py-2.5 bg-foreground text-background text-sm font-medium disabled:opacity-40 hover:opacity-80 transition-opacity"
                >
                  <RefreshCw className="w-4 h-4" /> Generate presentation plan
                </button>
              </div>
            </form>
          )}

          {/* ── Stage: planning (loading) ─────────────────────────────────── */}
          {stage === 'planning' && (
            <div className="border bg-white p-10 flex flex-col items-center gap-4 text-center">
              <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" />
              <div>
                <p className="text-sm font-semibold text-foreground">Analysing knowledge graph…</p>
                <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                  Claude is traversing concepts, relationships, patterns, and source evidence
                  to propose a slide-by-slide deck structure with full intelligence grounding.
                  This takes 30–90 seconds.
                </p>
                <div className="mt-3 flex flex-wrap justify-center gap-1.5">
                  {['Concepts', 'Relationships', 'Patterns', 'Evidence', 'Source documents'].map(item => (
                    <span key={item} className="text-[10px] px-2 py-0.5 border border-foreground/10 text-muted-foreground/70">
                      {item}
                    </span>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground/60 mt-3 tabular-nums">
                  {elapsed}s elapsed
                </p>
              </div>
            </div>
          )}

          {/* ── Stage: review ─────────────────────────────────────────────── */}
          {stage === 'review' && plan && (
            <>
              {error && (
                <div className="callout callout-amber text-sm mb-4">
                  <strong>Error:</strong> {error}
                </div>
              )}
              <PlanReview
                plan={plan}
                onPlanUpdated={handlePlanUpdated}
                onApprove={handleApprove}
                onStartOver={handleStartOver}
                approving={approving}
              />
            </>
          )}

          {/* ── Stage: generating (loading) ───────────────────────────────── */}
          {stage === 'generating' && (
            <div className="border bg-white p-10 flex flex-col items-center gap-4 text-center">
              <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" />
              <div>
                <p className="text-sm font-semibold text-foreground">Assembling PowerPoint…</p>
                <p className="text-xs text-muted-foreground mt-1">
                  Rendering the approved plan into the IBM Asset Kit template.
                  This takes 20–60 seconds.
                </p>
                <p className="text-xs text-muted-foreground/60 mt-2 tabular-nums">
                  {elapsed}s elapsed
                </p>
              </div>
            </div>
          )}

          {/* ── Stage: done ───────────────────────────────────────────────── */}
          {stage === 'done' && downloadReady && (
            <div className="space-y-4">
              <div className="border bg-white p-5 space-y-4">
                <div className="flex items-start gap-3">
                  <div className="w-9 h-9 bg-foreground/[0.05] border flex items-center justify-center flex-shrink-0">
                    <Presentation className="w-4 h-4 text-foreground" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-semibold text-foreground truncate">{downloadReady.title}</p>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      PowerPoint presentation · {downloadReady.sourceCount} source{downloadReady.sourceCount !== 1 ? 's' : ''} referenced
                    </p>
                  </div>
                  <a
                    href={downloadReady.url}
                    download={downloadReady.filename}
                    className="flex items-center gap-1.5 px-4 py-2 bg-foreground text-background text-xs font-medium hover:opacity-80 transition-opacity flex-shrink-0"
                  >
                    <FileDown className="w-3.5 h-3.5" />
                    Download .pptx
                  </a>
                </div>
                <div className="text-[11px] text-muted-foreground pt-3 border-t flex items-start gap-2">
                  <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-amber-500" />
                  <span>
                    Generated from your approved plan. Review all content before sharing with clients.
                    Sources are consolidated in the final slide of the deck.
                  </span>
                </div>
              </div>
              <div className="flex justify-start">
                <button
                  type="button"
                  onClick={handleStartOver}
                  className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
                >
                  <RotateCcw className="w-4 h-4" /> Generate another
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── Saved materials tab ───────────────────────────────────────────── */}
      {tab === 'list' && (
        <div>
          {listLoading ? (
            <div className="py-10 text-center text-sm text-muted-foreground">Loading…</div>
          ) : deliverables.length === 0 ? (
            <div className="py-14 text-center">
              <Presentation className="w-8 h-8 mx-auto text-muted-foreground mb-3" />
              <p className="text-sm font-semibold text-foreground mb-1">No client materials yet</p>
              <p className="text-xs text-muted-foreground">Switch to "Generate new" to create your first presentation.</p>
            </div>
          ) : (
            <div className="border divide-y">
              {deliverables.map(d => (
                <DeliverableRow
                  key={d.id}
                  d={d}
                  onDeleted={id => setDeliverables(prev => prev.filter(x => x.id !== id))}
                />
              ))}
            </div>
          )}
        </div>
      )}

    </main>
  );
}
