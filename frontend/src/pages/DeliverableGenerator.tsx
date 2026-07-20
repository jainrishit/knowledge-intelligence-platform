import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '@/api/client';
import type { Deliverable, DeliverableType, Workspace } from '@/types/api';
import {
  FileDown, Loader2, RefreshCw,
  Network, GitBranch, TrendingUp, Cpu, AlertCircle, Presentation, Trash2,
} from 'lucide-react';

type Tab = 'generate' | 'list';

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
    headline: 'Full client briefing for a new team',
    hasFocusArea: false,
  },
  {
    id: 'client_201',
    label: 'Client 201',
    headline: 'Deep consulting analysis for engagement teams',
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

// ── Saved Materials row component ────────────────────────────────────────────

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

// ── Main page ────────────────────────────────────────────────────────────────

export default function DeliverableGenerator() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [tab, setTab] = useState<Tab>('generate');
  const [delivType, setDelivType] = useState<DeliverableType>('client_101');
  const [focusArea, setFocusArea] = useState('');
  const [generating, setGenerating] = useState(false);
  const [downloadReady, setDownloadReady] = useState<{ url: string; filename: string; title: string; sourceCount: number } | null>(null);
  const [deliverables, setDeliverables] = useState<Deliverable[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [error, setError] = useState('');

  const selectedType = TYPES.find(t => t.id === delivType) ?? TYPES[0];

  const generate = async (e: React.FormEvent) => {
    e.preventDefault();
    setGenerating(true);
    setError('');
    if (downloadReady?.url) URL.revokeObjectURL(downloadReady.url);
    setDownloadReady(null);

    try {
      const result = await api.deliverables.create(
        wsId,
        delivType,
        delivType === 'executive_summary' && focusArea.trim() ? focusArea.trim() : undefined,
      );
      const url = URL.createObjectURL(result.blob);
      setDownloadReady({ url, filename: result.filename, title: result.title, sourceCount: result.sourceCount });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Generation failed.');
    } finally {
      setGenerating(false);
    }
  };

  const loadList = () => {
    setListLoading(true);
    api.deliverables.list(wsId).then(setDeliverables).finally(() => setListLoading(false));
  };

  useEffect(() => {
    api.workspaces.get(wsId).then(setWorkspace).catch(() => null);
  }, [wsId]);

  useEffect(() => {
    if (tab === 'list') loadList();
  }, [tab]);

  useEffect(() => {
    return () => { if (downloadReady?.url) URL.revokeObjectURL(downloadReady.url); };
  }, [downloadReady]);

  return (
    <main className="max-w-4xl mx-auto px-8 py-10">

      <div className="mb-7 pb-6 border-b">
        <h1 className="text-2xl font-semibold text-foreground">Client Material Generator</h1>
        <p className="text-sm text-muted-foreground mt-1.5 leading-relaxed">
          Generate first-draft client presentations directly from this workspace's knowledge graph.
          Output is a PowerPoint file (.pptx) grounded entirely in your source documents.
        </p>
      </div>

      <div className="seg-group mb-8">
        <button className={`seg-btn${tab === 'generate' ? ' active' : ''}`} onClick={() => setTab('generate')}>
          Generate new
        </button>
        <button className={`seg-btn${tab === 'list' ? ' active' : ''}`} onClick={() => { setTab('list'); }}>
          Saved materials
        </button>
      </div>

      <WorkspaceKnowledgeSummary ws={workspace} />

      {tab === 'generate' && (
        <div className="space-y-8">
          <form onSubmit={generate} className="border bg-white p-6 space-y-6">

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
                  The graph will be traversed from this topic outward. Leave blank to cover the most significant theme across the workspace.
                </p>
              </div>
            )}

            <div className="flex justify-end pt-1 border-t">
              <button
                type="submit"
                disabled={generating || (workspace?.concept_count ?? 0) === 0}
                className="flex items-center gap-2 px-5 py-2.5 bg-foreground text-background text-sm font-medium disabled:opacity-40 hover:opacity-80 transition-opacity"
              >
                {generating
                  ? <><Loader2 className="w-4 h-4 animate-spin" /> Generating presentation…</>
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

          {downloadReady && (
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
                  First-draft presentation. Review all content before sharing with clients.
                  Sources are consolidated in the final slide of the deck.
                </span>
              </div>
            </div>
          )}
        </div>
      )}

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
