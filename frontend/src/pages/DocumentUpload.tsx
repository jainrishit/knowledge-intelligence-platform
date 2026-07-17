import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '@/api/client';
import type { Document, Workspace } from '@/types/api';
import {
  Upload, CheckCircle, XCircle, Loader2, Clock,
  Trash2, FileText, Network, GitBranch, TrendingUp, Cpu, ArrowRight,
} from 'lucide-react';
import { FILE_TYPE_META, getFileTypeMeta } from '@/lib/fileTypes';

function FileTypeBadge({ fileType }: { fileType: string }) {
  const m = getFileTypeMeta(fileType);
  return (
    <span
      className="inline-flex items-center text-[10px] font-semibold px-1.5 py-0.5 rounded flex-shrink-0"
      style={{ background: m.bg, border: `1px solid ${m.border}`, color: m.text }}
    >
      {m.badge}
    </span>
  );
}

const PIPELINE_STEPS = [
  { key: 'parsed',    label: 'Parsed',     desc: 'Text extracted' },
  { key: 'concepts',  label: 'Concepts',   desc: 'Entities identified' },
  { key: 'relations', label: 'Relations',  desc: 'Links traced' },
  { key: 'graph',     label: 'Graph',      desc: 'Knowledge incremented' },
  { key: 'patterns',  label: 'Patterns',   desc: 'Patterns evaluated' },
];

const FORMAT_PIPELINE: Record<string, string[]> = {
  pdf:  ['Full text extraction', 'Section-aware chunking', 'Concept extraction', 'Relationship mapping', 'Pattern analysis'],
  docx: ['Paragraph extraction', 'Heading-aware chunking', 'Concept extraction', 'Relationship mapping', 'Pattern analysis'],
  pptx: ['Slide text extraction', 'Shape & table parsing', 'Concept extraction', 'Relationship mapping', 'Pattern analysis'],
  xlsx: ['Sheet schema analysis', 'Table detection', 'Row-level concept extraction', 'Relationship mapping', 'Pattern analysis'],
  xls:  ['Legacy sheet parsing', 'Table detection', 'Row-level concept extraction', 'Relationship mapping', 'Pattern analysis'],
  csv:  ['Column schema inferred', 'Data row extraction', 'Entity identification', 'Relationship mapping', 'Pattern analysis'],
};

function PipelineSteps({ status, fileType }: { status: Document['upload_status']; fileType: string }) {
  const completedCount =
    status === 'complete'   ? 5 :
    status === 'processing' ? 2 :
    0;

  const steps = FORMAT_PIPELINE[fileType?.toLowerCase()] ?? PIPELINE_STEPS.map(s => s.desc);

  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {steps.map((label, i) => {
        const done    = i < completedCount;
        const active  = status === 'processing' && i === completedCount;
        const waiting = !done && !active;
        return (
          <span
            key={i}
            className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded"
            style={{
              background: done ? '#f0fdf4' : active ? '#eff6ff' : '#f9fafb',
              border: `1px solid ${done ? '#bbf7d0' : active ? '#bfdbfe' : '#e5e7eb'}`,
              color: done ? '#15803d' : active ? '#1d4ed8' : '#9ca3af',
            }}
          >
            {done    && <CheckCircle className="w-2.5 h-2.5 flex-shrink-0" />}
            {active  && <Loader2 className="w-2.5 h-2.5 animate-spin flex-shrink-0" />}
            {waiting && <Clock className="w-2.5 h-2.5 flex-shrink-0" />}
            {label}
          </span>
        );
      })}
    </div>
  );
}

const STATUS_CONFIG = {
  pending:    { label: 'Queued',     icon: Clock,       color: 'text-muted-foreground' },
  processing: { label: 'Processing', icon: Loader2,     color: 'text-primary' },
  complete:   { label: 'Ready',      icon: CheckCircle, color: 'text-green-600' },
  failed:     { label: 'Failed',     icon: XCircle,     color: 'text-destructive' },
} as const;

function StatusBadge({ status }: { status: Document['upload_status'] }) {
  const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.pending;
  const Icon = cfg.icon;
  return (
    <span className={`flex items-center gap-1.5 text-xs font-medium ${cfg.color}`}>
      <Icon className={`w-3.5 h-3.5 ${status === 'processing' ? 'animate-spin' : ''}`} />
      {cfg.label}
    </span>
  );
}

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function KnowledgeStateBar({ ws, onExploreGraph }: { ws: Workspace | null; onExploreGraph: () => void }) {
  if (!ws) return null;
  const hasKnowledge = ws.concept_count > 0;
  const isProcessing = ws.document_count > 0 && ws.concept_count === 0;

  return (
    <div className="mb-7 border bg-white">
      <div className="px-4 py-2.5 border-b flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          {hasKnowledge ? (
            <>
              <Cpu className="w-3.5 h-3.5 text-green-500" />
              <span className="text-xs font-semibold text-green-700">Knowledge graph active</span>
              <span className="text-[10px] text-muted-foreground ml-1">
                v{ws.graph_version}{ws.graph_last_updated ? ` · updated ${timeAgo(ws.graph_last_updated)}` : ''}
              </span>
            </>
          ) : isProcessing ? (
            <>
              <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              <span className="text-xs font-medium text-amber-600">Building knowledge graph...</span>
            </>
          ) : (
            <>
              <span className="w-2 h-2 rounded-full bg-muted-foreground/30" />
              <span className="text-xs text-muted-foreground">No knowledge compiled yet — upload documents to begin</span>
            </>
          )}
        </div>
        {hasKnowledge && (
          <button
            onClick={onExploreGraph}
            className="flex items-center gap-1 text-[11px] text-primary hover:underline font-medium"
          >
            Explore graph <ArrowRight className="w-3 h-3" />
          </button>
        )}
      </div>
      <div className="grid grid-cols-4 divide-x text-center">
        <div className="py-3">
          <p className="text-lg font-bold text-foreground tabular-nums">{ws.document_count}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <FileText className="w-3 h-3" /> documents
          </p>
        </div>
        <div className="py-3">
          <p className="text-lg font-bold text-foreground tabular-nums">{ws.concept_count.toLocaleString()}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <Network className="w-3 h-3" /> concepts
          </p>
        </div>
        <div className="py-3">
          <p className="text-lg font-bold text-foreground tabular-nums">{ws.relationship_count}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <GitBranch className="w-3 h-3" /> relationships
          </p>
        </div>
        <div className="py-3">
          <p className="text-lg font-bold text-foreground tabular-nums">{ws.pattern_count}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center justify-center gap-1">
            <TrendingUp className="w-3 h-3" /> patterns
          </p>
        </div>
      </div>
    </div>
  );
}

function FormatBreakdown({ documents }: { documents: Document[] }) {
  const complete = documents.filter(d => d.upload_status === 'complete');
  if (complete.length === 0) return null;

  const byType: Record<string, number> = {};
  complete.forEach(d => {
    const ft = d.file_type?.toLowerCase() ?? 'other';
    byType[ft] = (byType[ft] ?? 0) + 1;
  });

  return (
    <div className="mb-5 flex items-center gap-2 flex-wrap">
      <span className="text-[11px] text-muted-foreground font-medium">Ingested formats:</span>
      {Object.entries(byType).map(([ft, count]) => {
        const m = getFileTypeMeta(ft);
        return (
          <span
            key={ft}
            className="inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded"
            style={{ background: m.bg, border: `1px solid ${m.border}`, color: m.text }}
          >
            {m.badge} <span className="font-normal opacity-70">×{count}</span>
          </span>
        );
      })}
    </div>
  );
}

export default function DocumentUpload() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const navigate = useNavigate();
  const wsId = Number(workspaceId);
  const [workspace, setWorkspace]   = useState<Workspace | null>(null);
  const [documents, setDocuments]   = useState<Document[]>([]);
  const [dragging, setDragging]     = useState(false);
  const [uploading, setUploading]   = useState(false);
  const [uploadError, setUploadError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollingRef   = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadAll = useCallback(async () => {
    const [docs, ws] = await Promise.all([
      api.documents.list(wsId),
      api.workspaces.get(wsId),
    ]);
    setDocuments(docs);
    setWorkspace(ws);
  }, [wsId]);

  useEffect(() => { loadAll(); }, [loadAll]);

  useEffect(() => {
    const hasProcessing = documents.some(d => d.upload_status === 'pending' || d.upload_status === 'processing');
    if (hasProcessing) {
      pollingRef.current = setInterval(loadAll, 3000);
    }
    return () => { if (pollingRef.current) clearInterval(pollingRef.current); };
  }, [documents, loadAll]);

  const handleFiles = async (files: File[]) => {
    const valid = files.filter(f => /\.(pdf|docx|pptx|xlsx|xls|csv)$/i.test(f.name));
    setUploadError('');
    if (!valid.length) {
      setUploadError('Only PDF, DOCX, PPTX, XLSX, XLS, and CSV files are accepted.');
      return;
    }
    setUploading(true);
    try {
      const created = await api.documents.upload(wsId, valid);
      setDocuments(prev => [...created, ...prev]);
    } catch (e: unknown) {
      setUploadError(`Upload failed: ${e instanceof Error ? e.message : 'Unknown error'}`);
    } finally {
      setUploading(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    handleFiles(Array.from(e.dataTransfer.files));
  };

  const handleDelete = async (id: number) => {
    await api.documents.delete(id);
    setDocuments(prev => prev.filter(d => d.id !== id));
    const ws = await api.workspaces.get(wsId);
    setWorkspace(ws);
  };

  const readyCount      = documents.filter(d => d.upload_status === 'complete').length;
  const processingCount = documents.filter(d => d.upload_status === 'pending' || d.upload_status === 'processing').length;

  return (
    <main className="max-w-3xl mx-auto px-8 py-10">

      <div className="mb-7 pb-6 border-b">
        <h1 className="text-2xl font-semibold text-foreground">Document Library</h1>
        <p className="text-sm text-muted-foreground mt-1.5 leading-relaxed">
          Upload consulting assets in any supported format. Every document is automatically compiled into
          the knowledge graph — concepts, relationships, and patterns extracted end-to-end.
        </p>
      </div>

      <KnowledgeStateBar ws={workspace} onExploreGraph={() => navigate(`/workspace/${wsId}/graph`)} />

      {/* Drop zone */}
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => fileInputRef.current?.click()}
        className={`border-2 border-dashed p-8 text-center cursor-pointer transition-colors mb-2 ${
          dragging
            ? 'border-foreground bg-muted/30'
            : 'border-border hover:border-muted-foreground/50 hover:bg-muted/20'
        }`}
      >
        <Upload className={`w-6 h-6 mx-auto mb-3 ${dragging ? 'text-foreground' : 'text-muted-foreground'}`} />
        {uploading ? (
          <p className="text-sm font-medium text-foreground">Uploading...</p>
        ) : (
          <>
            <p className="text-sm font-medium text-foreground mb-2">Drop files here, or click to browse</p>
            <div className="flex items-center justify-center gap-1.5 flex-wrap">
              {Object.entries(FILE_TYPE_META).map(([type, m]) => (
                <span
                  key={type}
                  className="text-[10px] font-semibold px-1.5 py-0.5 rounded"
                  style={{ background: m.bg, border: `1px solid ${m.border}`, color: m.text }}
                >
                  {m.badge}
                </span>
              ))}
            </div>
            <p className="text-[11px] text-muted-foreground mt-2">
              PDF · DOCX · PPTX · XLSX · XLS · CSV — each format has a tailored extraction pipeline
            </p>
          </>
        )}
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.docx,.pptx,.xlsx,.xls,.csv"
          multiple
          className="hidden"
          onChange={e => handleFiles(Array.from(e.target.files ?? []))}
        />
      </div>

      {uploadError && (
        <div className="callout callout-amber text-xs mb-4">{uploadError}</div>
      )}

      {documents.length > 0 && (
        <div className="flex items-center gap-6 mt-4 mb-2 text-xs text-muted-foreground py-2 border-b">
          <span>{documents.length} total</span>
          {readyCount > 0     && <span className="text-green-600 font-medium">{readyCount} ready</span>}
          {processingCount > 0 && <span className="text-primary font-medium">{processingCount} processing...</span>}
        </div>
      )}

      <FormatBreakdown documents={documents} />

      {documents.length === 0 ? (
        <div className="py-12 text-center">
          <FileText className="w-7 h-7 mx-auto text-muted-foreground mb-3" />
          <p className="text-sm font-medium text-foreground">No documents yet</p>
          <p className="text-xs text-muted-foreground mt-1">Drop your first file above to begin building the knowledge graph.</p>
        </div>
      ) : (
        <div className="border divide-y">
          {documents.map(doc => (
            <div
              key={doc.id}
              className="px-4 py-4 bg-white hover:bg-muted/20 group transition-colors"
            >
              <div className="flex items-start gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <FileTypeBadge fileType={doc.file_type} />
                    <span className="font-medium text-sm text-foreground truncate">
                      {doc.title || doc.filename.split('/').pop()}
                    </span>
                  </div>

                  {/* Format-specific pipeline steps */}
                  {(doc.upload_status === 'processing' || doc.upload_status === 'pending' || doc.upload_status === 'complete') && (
                    <PipelineSteps status={doc.upload_status} fileType={doc.file_type} />
                  )}

                  {/* Provenance: topics + industry after completion */}
                  {doc.upload_status === 'complete' && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {doc.industry && (
                        <span className="type-pill type-General">{doc.industry}</span>
                      )}
                      {doc.topics.slice(0, 5).map(t => (
                        <span key={t} className="text-[11px] bg-muted text-muted-foreground border px-2 py-0.5 rounded">
                          {t}
                        </span>
                      ))}
                    </div>
                  )}

                  {doc.error_message && (
                    <p className="text-[11px] text-destructive mt-1">{doc.error_message}</p>
                  )}
                </div>

                <div className="flex items-center gap-4 flex-shrink-0 mt-0.5">
                  <StatusBadge status={doc.upload_status} />
                  <button
                    onClick={() => handleDelete(doc.id)}
                    className="p-1 text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
                    title="Delete document"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
