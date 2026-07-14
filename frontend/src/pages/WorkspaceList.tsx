import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '@/api/client';
import type { Workspace } from '@/types/api';
import { Plus, Trash2, FileText, Network, ChevronRight, BookOpen, MessageSquare, FileOutput } from 'lucide-react';

const JOURNEY = [
  { icon: BookOpen,       label: 'Upload Documents',   desc: 'PDFs, DOCX, PPTX' },
  { icon: Network,        label: 'Build Knowledge Graph', desc: 'Concepts & relationships extracted' },
  { icon: MessageSquare,  label: 'Ask the Assistant',  desc: 'Evidence-backed answers only' },
  { icon: FileOutput,     label: 'Generate Deliverables', desc: 'POVs, summaries, roadmaps' },
];

export default function WorkspaceList() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [showForm, setShowForm] = useState(false);
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
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-widest mb-2">IBM Consulting · Knowledge Intelligence Platform</p>
        <h1 className="text-3xl font-semibold text-foreground leading-tight">Workspaces</h1>
        <p className="text-sm text-muted-foreground mt-2 max-w-2xl leading-relaxed">
          A workspace is a self-contained knowledge domain. Upload your consulting assets (PDFs, Word docs, PowerPoints)
          and the platform automatically extracts every concept, relationship, and pattern, then links each finding back to
          its exact source paragraph. Nothing is fabricated.
        </p>
      </div>

      <div className="mb-10 border bg-white p-5">
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-widest mb-4">How it works</p>
        <div className="grid grid-cols-4 gap-0">
          {JOURNEY.map((step, i) => {
            const Icon = step.icon;
            return (
              <div key={i} className="flex items-start gap-3 relative">
                {i < JOURNEY.length - 1 && (
                  <ChevronRight className="absolute right-0 top-2.5 w-3.5 h-3.5 text-border" />
                )}
                <span className="step-dot step-dot-pending text-[11px] mt-0.5">{i + 1}</span>
                <div>
                  <div className="flex items-center gap-1.5 mb-0.5">
                    <Icon className="w-3.5 h-3.5 text-muted-foreground" />
                    <p className="text-xs font-semibold text-foreground">{step.label}</p>
                  </div>
                  <p className="text-[11px] text-muted-foreground leading-snug">{step.desc}</p>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="flex items-center justify-between mb-5">
        <h2 className="text-sm font-semibold text-foreground">
          {workspaces.length > 0 ? `${workspaces.length} workspace${workspaces.length > 1 ? 's' : ''}` : 'Your workspaces'}
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
              <label className="block text-xs font-medium text-muted-foreground mb-1.5">Workspace name <span className="text-destructive">*</span></label>
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
              <label className="block text-xs font-medium text-muted-foreground mb-1.5">Description <span className="text-muted-foreground font-normal">(optional)</span></label>
              <input
                className="w-full border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white"
                placeholder="What topic or project does this workspace cover?"
                value={newDesc}
                onChange={e => setNewDesc(e.target.value)}
              />
            </div>
            {createError && (
              <div className="callout callout-amber text-xs">
                {createError}
              </div>
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
            <div
              key={ws.id}
              onClick={() => navigate(`/workspace/${ws.id}/documents`)}
              className="bg-white p-6 cursor-pointer hover:bg-muted/40 transition-colors group relative"
            >
              <button
                onClick={e => handleDelete(ws.id, e)}
                className="absolute top-4 right-4 p-1.5 text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
                title="Delete workspace"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>

              <h3 className="font-semibold text-foreground text-[15px] mb-1 pr-8 leading-tight">{ws.name}</h3>
              {ws.description && (
                <p className="text-xs text-muted-foreground mb-4 line-clamp-2 leading-relaxed">{ws.description}</p>
              )}

              <div className="flex items-center gap-5 mt-4 text-xs text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <FileText className="w-3.5 h-3.5" />
                  {ws.document_count} {ws.document_count === 1 ? 'doc' : 'docs'}
                </span>
                <span className="flex items-center gap-1.5">
                  <Network className="w-3.5 h-3.5" />
                  {ws.concept_count} {ws.concept_count === 1 ? 'concept' : 'concepts'}
                </span>
                <span className="ml-auto text-[11px]">
                  {new Date(ws.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                </span>
              </div>

              {ws.document_count === 0 && (
                <div className="mt-4 flex items-center gap-1.5 text-[11px] text-primary font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-primary" />
                  Start by uploading documents
                </div>
              )}
              {ws.document_count > 0 && ws.concept_count === 0 && (
                <div className="mt-4 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                  Processing documents...
                </div>
              )}
              {ws.concept_count > 0 && (
                <div className="mt-4 flex items-center gap-1.5 text-[11px] text-green-600 font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
                  Knowledge graph ready
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
