import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '@/api/client';
import type { Deliverable, DeliverableResponse } from '@/types/api';
import { FileDown, Loader2, RefreshCw, Edit2, Eye, FileText } from 'lucide-react';

type Tab = 'generate' | 'list';
type DeliverableType = 'POV' | 'executive_summary' | 'roadmap';

const TYPES: { id: DeliverableType; label: string; headline: string; desc: string; sections: string[] }[] = [
  {
    id: 'POV',
    label: 'Point of View',
    headline: 'IBM\'s perspective on a topic',
    desc: 'A concise 1–2 page document articulating IBM\'s stance, recommendation, and rationale. Best used to brief a client executive or open a conversation.',
    sections: ['Executive Summary', 'Context', 'IBM Recommendation', 'Architecture Considerations', 'Roadmap', 'Risks'],
  },
  {
    id: 'executive_summary',
    label: 'Executive Summary',
    headline: 'High-level synthesis for leadership',
    desc: 'A condensed overview of findings, insights, and recommended next steps. Designed for a C-suite reader who needs the essentials in under 5 minutes.',
    sections: ['Summary', 'Key Findings', 'Recommended Actions', 'Supporting Evidence'],
  },
  {
    id: 'roadmap',
    label: 'Roadmap',
    headline: 'Phased implementation plan',
    desc: 'A structured, phase-by-phase plan showing how to move from current state to target state. Includes milestones, dependencies, and risks per phase.',
    sections: ['Vision', 'Current State', 'Phase 1–3 Plan', 'Key Milestones', 'Risks & Mitigations'],
  },
];

function renderMarkdown(md: string): string {
  return md
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, s => `<ul>${s}</ul>`)
    .replace(/\n\n/g, '</p><p>')
    .replace(/^(?!<[hul])/gm, '')
    .split('\n')
    .map(line => line.startsWith('<') ? line : line ? `<p>${line}</p>` : '')
    .join('\n');
}

export default function DeliverableGenerator() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);
  const [tab, setTab] = useState<Tab>('generate');
  const [delivType, setDelivType] = useState<DeliverableType>('POV');
  const [topic, setTopic] = useState('');
  const [audience, setAudience] = useState('CIO');
  const [generating, setGenerating] = useState(false);
  const [result, setResult] = useState<DeliverableResponse | null>(null);
  const [editContent, setEditContent] = useState('');
  const [previewMode, setPreviewMode] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [deliverables, setDeliverables] = useState<Deliverable[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [error, setError] = useState('');

  const selectedType = TYPES.find(t => t.id === delivType) ?? TYPES[0];

  const generate = async (e: React.FormEvent) => {
    e.preventDefault();
    setGenerating(true);
    setError('');
    setResult(null);
    try {
      const resp = await api.deliverables.create(wsId, delivType, topic || undefined, audience || undefined);
      setResult(resp);
      setEditContent(resp.deliverable.content_markdown || '');
      setPreviewMode(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Generation failed.');
    } finally {
      setGenerating(false);
    }
  };

  const save = async () => {
    if (!result) return;
    setSaving(true);
    try {
      await api.deliverables.update(result.deliverable.id, editContent);
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } finally {
      setSaving(false);
    }
  };

  const loadList = () => {
    setListLoading(true);
    api.deliverables.list(wsId).then(setDeliverables).finally(() => setListLoading(false));
  };

  useEffect(() => {
    if (tab === 'list') loadList();
  }, [tab]);

  return (
    <main className="max-w-4xl mx-auto px-8 py-10">

      <div className="mb-7 pb-6 border-b">
        <h1 className="text-2xl font-semibold text-foreground">Deliverable Generator</h1>
        <p className="text-sm text-muted-foreground mt-1.5 leading-relaxed">
          Generate client-ready consulting documents directly from this workspace's compiled knowledge.
          Every section is grounded in your source documents. No fabricated content.
        </p>
      </div>

      <div className="seg-group mb-8">
        <button className={`seg-btn${tab === 'generate' ? ' active' : ''}`} onClick={() => setTab('generate')}>
          Generate new
        </button>
        <button className={`seg-btn${tab === 'list' ? ' active' : ''}`} onClick={() => setTab('list')}>
          Saved deliverables
        </button>
      </div>

      {tab === 'generate' && (
        <div className="space-y-8">
          <form onSubmit={generate} className="border bg-white p-6 space-y-6">

            <div>
              <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">
                Document type
              </label>
              <div className="grid grid-cols-3 gap-3">
                {TYPES.map(t => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => setDelivType(t.id)}
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

              {/* Type explainer */}
              <div className="callout callout-green mt-3 text-xs">
                <p className="font-medium text-foreground mb-1">{selectedType.label}: what's included</p>
                <p className="text-muted-foreground mb-2">{selectedType.desc}</p>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {selectedType.sections.map(s => (
                    <span key={s} className="bg-white border px-2 py-0.5 rounded text-[10px] text-foreground font-medium">
                      {s}
                    </span>
                  ))}
                </div>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                  Topic / focus area
                </label>
                <input
                  className="w-full border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white"
                  placeholder="e.g. ISO 20022 Migration"
                  value={topic}
                  onChange={e => setTopic(e.target.value)}
                />
                <p className="text-[11px] text-muted-foreground mt-1">Leave blank to cover all workspace knowledge</p>
              </div>
              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                  Target audience
                </label>
                <input
                  className="w-full border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white"
                  placeholder="e.g. CIO, CFO, Board"
                  value={audience}
                  onChange={e => setAudience(e.target.value)}
                />
                <p className="text-[11px] text-muted-foreground mt-1">Shapes the tone and depth of the document</p>
              </div>
            </div>

            <div className="flex justify-end pt-1 border-t">
              <button
                type="submit"
                disabled={generating}
                className="flex items-center gap-2 px-5 py-2.5 bg-foreground text-background text-sm font-medium disabled:opacity-40 hover:opacity-80 transition-opacity"
              >
                {generating
                  ? <><Loader2 className="w-4 h-4 animate-spin" /> Generating...</>
                  : <><RefreshCw className="w-4 h-4" /> Generate {selectedType.label}</>
                }
              </button>
            </div>
          </form>

          {error && (
            <div className="callout callout-amber text-sm">
              <strong>Generation failed:</strong> {error}
            </div>
          )}

          {result && (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-foreground">{result.deliverable.title}</h2>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {result.deliverable.type.replace('_', ' ')} &middot; {result.sources.length} source{result.sources.length !== 1 ? 's' : ''}
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => setPreviewMode(v => !v)}
                    className="flex items-center gap-1.5 px-3 py-1.5 border text-xs font-medium hover:bg-muted transition-colors"
                  >
                    {previewMode ? <><Edit2 className="w-3.5 h-3.5" /> Edit</> : <><Eye className="w-3.5 h-3.5" /> Preview</>}
                  </button>
                  <button
                    onClick={save}
                    disabled={saving}
                    className="px-3 py-1.5 border text-xs font-medium hover:bg-muted transition-colors"
                  >
                    {saving ? 'Saving...' : saved ? 'Saved ✓' : 'Save'}
                  </button>
                  <a
                    href={api.deliverables.exportUrl(result.deliverable.id, 'md')}
                    download
                    className="flex items-center gap-1.5 px-3 py-1.5 border text-xs font-medium hover:bg-muted transition-colors"
                  >
                    <FileDown className="w-3.5 h-3.5" />
                    .md
                  </a>
                  <a
                    href={api.deliverables.exportUrl(result.deliverable.id, 'docx')}
                    download
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-foreground text-background text-xs font-medium hover:opacity-80 transition-opacity"
                  >
                    <FileDown className="w-3.5 h-3.5" />
                    .docx
                  </a>
                </div>
              </div>

              {previewMode ? (
                <div
                  className="prose-preview border bg-white p-6 min-h-[400px] max-h-[600px] overflow-y-auto"
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(editContent) }}
                />
              ) : (
                <textarea
                  className="w-full min-h-[400px] border p-4 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-foreground bg-white leading-relaxed resize-y"
                  value={editContent}
                  onChange={e => setEditContent(e.target.value)}
                />
              )}

              {result.sources.length > 0 && (
                <div className="border bg-white p-4">
                  <div className="flex items-center gap-1.5 mb-3">
                    <FileText className="w-3.5 h-3.5 text-muted-foreground" />
                    <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                      Source documents ({result.sources.length})
                    </p>
                  </div>
                  <div className="space-y-2">
                    {result.sources.map((s, i) => (
                      <div key={i} className="flex items-start gap-2.5 text-xs">
                        <span className="w-4 h-4 rounded bg-muted flex items-center justify-center text-[9px] font-bold text-muted-foreground flex-shrink-0 mt-0.5">
                          {i + 1}
                        </span>
                        <div>
                          <p className="font-medium text-foreground">{s.document_name}</p>
                          {s.excerpt && (
                            <p className="text-muted-foreground italic mt-0.5 leading-relaxed">"{s.excerpt.slice(0, 150)}{s.excerpt.length > 150 ? '...' : ''}"</p>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'list' && (
        <div>
          {listLoading ? (
            <div className="py-10 text-center text-sm text-muted-foreground">Loading...</div>
          ) : deliverables.length === 0 ? (
            <div className="py-14 text-center">
              <FileText className="w-8 h-8 mx-auto text-muted-foreground mb-3" />
              <p className="text-sm font-semibold text-foreground mb-1">No deliverables yet</p>
              <p className="text-xs text-muted-foreground">Switch to "Generate new" to create your first document.</p>
            </div>
          ) : (
            <div className="border divide-y">
              {deliverables.map(d => (
                <div key={d.id} className="bg-white px-5 py-4 flex items-center gap-4 hover:bg-muted/20 transition-colors">
                  <div className="flex-shrink-0">
                    <span className="type-pill type-General">
                      {d.type.replace('_', ' ')}
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-sm text-foreground truncate">{d.title}</p>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {new Date(d.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                      {d.source_document_ids.length > 0 && ` · ${d.source_document_ids.length} source${d.source_document_ids.length > 1 ? 's' : ''}`}
                    </p>
                  </div>
                  <div className="flex gap-2 flex-shrink-0">
                    <a
                      href={api.deliverables.exportUrl(d.id, 'md')}
                      download
                      className="flex items-center gap-1 text-xs border px-2.5 py-1.5 hover:bg-muted transition-colors"
                    >
                      <FileDown className="w-3 h-3" /> .md
                    </a>
                    <a
                      href={api.deliverables.exportUrl(d.id, 'docx')}
                      download
                      className="flex items-center gap-1 text-xs bg-foreground text-background px-2.5 py-1.5 hover:opacity-80 transition-opacity"
                    >
                      <FileDown className="w-3 h-3" /> .docx
                    </a>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </main>
  );
}
