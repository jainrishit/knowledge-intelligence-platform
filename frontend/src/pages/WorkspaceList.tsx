import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '@/api/client';
import type { Workspace } from '@/types/api';
import {
  Plus, Trash2, FileText, Network, ChevronRight,
  GitBranch, Cpu, TrendingUp, Database, BarChart2,
} from 'lucide-react';

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return 'never';
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

const FORMAT_COLOURS: Record<string, { bg: string; border: string; text: string }> = {
  pdf:  { bg: '#fff1f2', border: '#f87171', text: '#b91c1c' },
  docx: { bg: '#eff6ff', border: '#60a5fa', text: '#1d4ed8' },
  pptx: { bg: '#fff7ed', border: '#fb923c', text: '#c2410c' },
  xlsx: { bg: '#f0fdf4', border: '#4ade80', text: '#15803d' },
  xls:  { bg: '#f0fdf4', border: '#4ade80', text: '#15803d' },
  csv:  { bg: '#f8fafc', border: '#94a3b8', text: '#334155' },
};

function PlatformIntelligenceBar({ workspaces }: { workspaces: Workspace[] }) {
  const totalDocs      = workspaces.reduce((s, w) => s + w.document_count, 0);
  const totalConcepts  = workspaces.reduce((s, w) => s + w.concept_count, 0);
  const totalRels      = workspaces.reduce((s, w) => s + w.relationship_count, 0);
  const totalPatterns  = workspaces.reduce((s, w) => s + w.pattern_count, 0);
  const activeGraphs   = workspaces.filter(w => w.concept_count > 0).length;

  if (totalDocs === 0 && totalConcepts === 0) return null;

  return (
    <div className="mb-8 border bg-white">
      <div className="px-5 py-2.5 border-b flex items-center gap-2">
        <BarChart2 className="w-3.5 h-3.5 text-primary" />
        <span className="text-xs font-semibold text-foreground uppercase tracking-wider">Platform Intelligence</span>
        {activeGraphs > 0 && (
          <span className="ml-auto flex items-center gap-1 text-[10px] text-green-600 font-medium">
            <Cpu className="w-3 h-3 text-green-500" />
            {activeGraphs} graph{activeGraphs > 1 ? 's' : ''} active in memory
          </span>
        )}
      </div>
      <div className="grid grid-cols-4 divide-x">
        <div className="px-5 py-4 text-center">
          <p className="text-2xl font-bold text-foreground tabular-nums">{totalDocs}</p>
          <p className="text-[11px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <FileText className="w-3 h-3" /> documents ingested
          </p>
        </div>
        <div className="px-5 py-4 text-center">
          <p className="text-2xl font-bold text-foreground tabular-nums">{totalConcepts.toLocaleString()}</p>
          <p className="text-[11px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <Network className="w-3 h-3" /> concepts compiled
          </p>
        </div>
        <div className="px-5 py-4 text-center">
          <p className="text-2xl font-bold text-foreground tabular-nums">{totalRels.toLocaleString()}</p>
          <p className="text-[11px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <GitBranch className="w-3 h-3" /> relationships mapped
          </p>
        </div>
        <div className="px-5 py-4 text-center">
          <p className="text-2xl font-bold text-foreground tabular-nums">{totalPatterns}</p>
          <p className="text-[11px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <TrendingUp className="w-3 h-3" /> patterns identified
          </p>
        </div>
      </div>
    </div>
  );
}

function WorkspaceCard({ ws, onDelete, onClick }: {
  ws: Workspace;
  onDelete: (id: number, e: React.MouseEvent) => void;
  onClick: () => void;
}) {
  const hasGraph    = ws.concept_count > 0;
  const isProcessing = ws.document_count > 0 && ws.concept_count === 0;

  const knowledgePct = hasGraph
    ? Math.min(100, Math.round((ws.concept_count / Math.max(ws.concept_count, 200)) * 100))
    : 0;

  return (
    <div
      onClick={onClick}
      className="bg-white p-5 cursor-pointer hover:bg-muted/30 transition-colors group relative flex flex-col gap-0 border-0"
    >
      <button
        onClick={e => onDelete(ws.id, e)}
        className="absolute top-4 right-4 p-1.5 text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
        title="Delete workspace"
      >
        <Trash2 className="w-3.5 h-3.5" />
      </button>

      {/* Name + description */}
      <div className="flex items-start gap-2 mb-2 pr-8">
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-foreground text-[15px] leading-tight truncate">{ws.name}</h3>
          {ws.description && (
            <p className="text-xs text-muted-foreground mt-0.5 line-clamp-1 leading-relaxed">{ws.description}</p>
          )}
        </div>
        {hasGraph && (
          <span className="flex-shrink-0 flex items-center gap-1 text-[10px] px-2 py-0.5 bg-green-50 border border-green-200 text-green-700 rounded-full font-medium">
            <Cpu className="w-2.5 h-2.5" />
            Active
          </span>
        )}
        {isProcessing && (
          <span className="flex-shrink-0 flex items-center gap-1 text-[10px] px-2 py-0.5 bg-amber-50 border border-amber-200 text-amber-700 rounded-full font-medium">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
            Building
          </span>
        )}
      </div>

      {/* Stat row */}
      <div className="flex items-center gap-3 text-[11px] text-muted-foreground mb-3">
        <span className="flex items-center gap-1">
          <FileText className="w-3 h-3" />
          {ws.document_count} {ws.document_count === 1 ? 'doc' : 'docs'}
        </span>
        <span className="flex items-center gap-1">
          <Network className="w-3 h-3" />
          {ws.concept_count.toLocaleString()} concepts
        </span>
        {ws.relationship_count > 0 && (
          <span className="flex items-center gap-1">
            <GitBranch className="w-3 h-3" />
            {ws.relationship_count} links
          </span>
        )}
        {ws.pattern_count > 0 && (
          <span className="flex items-center gap-1 text-primary font-medium">
            <TrendingUp className="w-3 h-3" />
            {ws.pattern_count} patterns
          </span>
        )}
      </div>

      {/* Knowledge compilation progress bar */}
      {(hasGraph || isProcessing) && (
        <div className="mb-3">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] text-muted-foreground">Knowledge compiled</span>
            {hasGraph && (
              <span className="text-[10px] text-muted-foreground">
                v{ws.graph_version} · {timeAgo(ws.graph_last_updated)}
              </span>
            )}
          </div>
          <div className="h-1 w-full bg-muted rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${isProcessing ? 'bg-amber-400 animate-pulse' : 'bg-green-500'}`}
              style={{ width: isProcessing ? '35%' : `${knowledgePct}%` }}
            />
          </div>
        </div>
      )}

      {/* Quick nav links */}
      {hasGraph && (
        <div className="flex items-center gap-2 mt-1 pt-2 border-t border-border/50">
          <span className="text-[10px] text-muted-foreground mr-1">Go to:</span>
          {[
            { label: 'Graph', path: 'graph' },
            { label: 'Assistant', path: 'chat' },
            { label: 'Deliverables', path: 'deliverables' },
          ].map(link => (
            <button
              key={link.path}
              onClick={e => { e.stopPropagation(); onClick(); }}
              className="text-[10px] px-2 py-0.5 border rounded text-muted-foreground hover:text-foreground hover:border-foreground/40 transition-colors"
            >
              {link.label}
              <ChevronRight className="w-2.5 h-2.5 inline ml-0.5 -mt-0.5" />
            </button>
          ))}
        </div>
      )}

      {ws.document_count === 0 && (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-primary font-medium">
          <Database className="w-3 h-3" />
          Upload documents to start building knowledge
        </div>
      )}
    </div>
  );
}

export default function WorkspaceList() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading]       = useState(true);
  const [loadError, setLoadError]   = useState('');
  const [creating, setCreating]     = useState(false);
  const [createError, setCreateError] = useState('');
  const [newName, setNewName]       = useState('');
  const [newDesc, setNewDesc]       = useState('');
  const [showForm, setShowForm]     = useState(false);
  const navigate = useNavigate();

  const load = () => {
    setLoading(true);
    setLoadError('');
    api.workspaces.list()
      .then(setWorkspaces)
      .catch(err => setLoadError(err.message ?? 'Could not reach the backend. Is the server running?'))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    setCreateError('');
    try {
      const ws = await api.workspaces.create(newName.trim(), newDesc.trim() || undefined);
      setWorkspaces(prev => [ws, ...prev]);
      setNewName('');
      setNewDesc('');
      setShowForm(false);
    } catch (err: unknown) {
      setCreateError(err instanceof Error ? err.message : 'Create failed. Is the backend running on port 8000?');
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: number, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Delete this workspace and all its knowledge? This cannot be undone.')) return;
    await api.workspaces.delete(id);
    setWorkspaces(prev => prev.filter(w => w.id !== id));
  };

  return (
    <main className="max-w-5xl mx-auto px-8 py-10">

      <div className="mb-8">
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-widest mb-2">
          IBM Consulting · Knowledge Intelligence Platform
        </p>
        <h1 className="text-3xl font-semibold text-foreground leading-tight">Workspaces</h1>
        <p className="text-sm text-muted-foreground mt-2 max-w-2xl leading-relaxed">
          Each workspace is an isolated knowledge domain. Upload consulting assets across any format — the
          platform automatically compiles every concept, relationship, and pattern into a persistent, searchable
          knowledge graph. Every insight is traceable to source evidence.
        </p>
      </div>

      {!loading && <PlatformIntelligenceBar workspaces={workspaces} />}

      <div className="flex items-center justify-between mb-5">
        <h2 className="text-sm font-semibold text-foreground">
          {workspaces.length > 0
            ? `${workspaces.length} workspace${workspaces.length > 1 ? 's' : ''}`
            : 'Your workspaces'}
        </h2>
        <button
          onClick={() => setShowForm(v => !v)}
          className="flex items-center gap-2 px-4 py-2 bg-foreground text-background text-sm font-medium hover:opacity-80 transition-opacity"
        >
          <Plus className="w-4 h-4" />
          New workspace
        </button>
      </div>

      {showForm && (
        <div className="mb-6 border bg-white p-6">
          <h3 className="text-sm font-semibold text-foreground mb-1">Create a new workspace</h3>
          <p className="text-xs text-muted-foreground mb-4">
            Give it a clear domain name, e.g. "Payments Modernization" or "Digital Assets Strategy".
            Documents uploaded here will never mix with other workspaces.
          </p>
          <form onSubmit={handleCreate} className="flex flex-col gap-3 max-w-md">
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1.5">
                Workspace name <span className="text-destructive">*</span>
              </label>
              <input
                className="w-full border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white"
                placeholder="e.g. Payments Modernization"
                value={newName}
                onChange={e => setNewName(e.target.value)}
                required
                autoFocus
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1.5">
                Description <span className="text-muted-foreground font-normal">(optional)</span>
              </label>
              <input
                className="w-full border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white"
                placeholder="What topic or project does this workspace cover?"
                value={newDesc}
                onChange={e => setNewDesc(e.target.value)}
              />
            </div>
            {createError && (
              <div className="callout callout-amber text-xs">{createError}</div>
            )}
            <div className="flex gap-2 pt-1">
              <button
                type="submit"
                disabled={creating || !newName.trim()}
                className="px-4 py-2 bg-foreground text-background text-sm font-medium disabled:opacity-40 hover:opacity-80 transition-opacity"
              >
                {creating ? 'Creating...' : 'Create workspace'}
              </button>
              <button
                type="button"
                onClick={() => { setShowForm(false); setCreateError(''); }}
                className="px-4 py-2 border text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {loadError && (
        <div className="mb-6 callout callout-amber text-sm">
          <strong>Cannot reach backend:</strong> {loadError}
        </div>
      )}

      {loading ? (
        <div className="py-20 text-center text-sm text-muted-foreground">Loading workspaces...</div>
      ) : workspaces.length === 0 && !loadError ? (
        <div className="py-16 text-center border border-dashed">
          <Network className="w-8 h-8 mx-auto text-muted-foreground mb-4" />
          <p className="text-sm font-semibold text-foreground mb-1">No workspaces yet</p>
          <p className="text-xs text-muted-foreground max-w-xs mx-auto">
            Click "New workspace" above to create your first knowledge domain and start uploading documents.
          </p>
        </div>
      ) : (
        <div className="grid gap-px bg-border sm:grid-cols-2 border">
          {workspaces.map(ws => (
            <WorkspaceCard
              key={ws.id}
              ws={ws}
              onDelete={handleDelete}
              onClick={() => navigate(`/workspace/${ws.id}/documents`)}
            />
          ))}
        </div>
      )}
    </main>
  );
}
