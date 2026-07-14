import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '@/api/client';
import type { Document } from '@/types/api';
import { Upload, CheckCircle, XCircle, Loader2, Clock, Trash2, FileText, Info } from 'lucide-react';

const STATUS_CONFIG = {
  pending:    { label: 'Queued',     icon: Clock,        color: 'text-muted-foreground' },
  processing: { label: 'Processing', icon: Loader2,      color: 'text-primary' },
  complete:   { label: 'Ready',      icon: CheckCircle,  color: 'text-green-600' },
  failed:     { label: 'Failed',     icon: XCircle,      color: 'text-destructive' },
} as const;

const PIPELINE_STEPS = [
  { label: 'Text extraction',   desc: 'Raw text pulled from every page/slide' },
  { label: 'Concept detection', desc: 'Named entities, standards, technologies identified' },
  { label: 'Relationship mapping', desc: 'Links between concepts traced across the document' },
  { label: 'Pattern recognition', desc: 'Recurring consulting patterns grouped and labelled' },
];

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

export default function DocumentUpload() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    const docs = await api.documents.list(wsId);
    setDocuments(docs);
  }, [wsId]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const hasProcessing = documents.some(d => d.upload_status === 'pending' || d.upload_status === 'processing');
    if (hasProcessing) {
      pollingRef.current = setInterval(load, 3000);
    }
    return () => { if (pollingRef.current) clearInterval(pollingRef.current); };
  }, [documents, load]);

  const handleFiles = async (files: File[]) => {
    const valid = files.filter(f => /\.(pdf|docx|pptx)$/i.test(f.name));
    setUploadError('');
    if (!valid.length) {
      setUploadError('Only PDF, DOCX, and PPTX files are accepted.');
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
    if (!confirm('Remove this document and its extracted knowledge?')) return;
    await api.documents.delete(id);
    setDocuments(prev => prev.filter(d => d.id !== id));
  };

  const readyCount = documents.filter(d => d.upload_status === 'complete').length;
  const processingCount = documents.filter(d => d.upload_status === 'pending' || d.upload_status === 'processing').length;

  return (
    <main className="max-w-3xl mx-auto px-8 py-10">

      <div className="mb-7 pb-6 border-b">
        <h1 className="text-2xl font-semibold text-foreground">Document Library</h1>
        <p className="text-sm text-muted-foreground mt-1.5 leading-relaxed">
          Upload your consulting assets. Every document is automatically parsed and run through four extraction
          stages with no manual tagging required. Results are always traceable back to the exact source text.
        </p>
      </div>

      <div className="mb-7 border bg-white p-4">
        <div className="flex items-center gap-1.5 mb-3">
          <Info className="w-3.5 h-3.5 text-primary" />
          <p className="text-xs font-semibold text-foreground">What happens after you upload</p>
        </div>
        <div className="grid grid-cols-4 gap-3">
          {PIPELINE_STEPS.map((s, i) => (
            <div key={i} className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5">
                <span className="step-dot step-dot-pending text-[10px]">{i + 1}</span>
                <p className="text-[11px] font-semibold text-foreground">{s.label}</p>
              </div>
              <p className="text-[11px] text-muted-foreground leading-snug pl-7">{s.desc}</p>
            </div>
          ))}
        </div>
      </div>

      <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => fileInputRef.current?.click()}
        className={`border-2 border-dashed p-10 text-center cursor-pointer transition-colors mb-2 ${
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
            <p className="text-sm font-medium text-foreground">Drop files here, or click to browse</p>
            <p className="text-xs text-muted-foreground mt-1">Accepts PDF, DOCX, PPTX</p>
          </>
        )}
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.docx,.pptx"
          multiple
          className="hidden"
          onChange={e => handleFiles(Array.from(e.target.files ?? []))}
        />
      </div>

      {uploadError && (
        <div className="callout callout-amber text-xs mb-4">{uploadError}</div>
      )}

      {documents.length > 0 && (
        <div className="flex items-center gap-6 mb-4 text-xs text-muted-foreground py-2 border-b">
          <span>{documents.length} total</span>
          {readyCount > 0 && <span className="text-green-600 font-medium">{readyCount} ready</span>}
          {processingCount > 0 && <span className="text-primary font-medium">{processingCount} processing...</span>}
        </div>
      )}

      {documents.length === 0 ? (
        <div className="py-12 text-center">
          <FileText className="w-7 h-7 mx-auto text-muted-foreground mb-3" />
          <p className="text-sm font-medium text-foreground">No documents yet</p>
          <p className="text-xs text-muted-foreground mt-1">Drop your first PDF, DOCX, or PPTX above to begin.</p>
        </div>
      ) : (
        <div className="border divide-y">
          {documents.map(doc => (
            <div key={doc.id} className="flex items-start gap-3 px-4 py-4 bg-white hover:bg-muted/20 group transition-colors">
              <FileText className="w-4 h-4 text-muted-foreground mt-0.5 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span className="font-medium text-sm text-foreground truncate">
                    {doc.title || doc.filename.split('/').pop()}
                  </span>
                  <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold border px-1.5 py-0.5 flex-shrink-0">
                    {doc.file_type}
                  </span>
                </div>

                {doc.upload_status === 'complete' && (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {doc.industry && (
                      <span className="type-pill type-General">
                        {doc.industry}
                      </span>
                    )}
                    {doc.topics.slice(0, 6).map(t => (
                      <span key={t} className="text-[11px] bg-muted text-muted-foreground border px-2 py-0.5 rounded">
                        {t}
                      </span>
                    ))}
                  </div>
                )}

                {doc.upload_status === 'processing' && (
                  <p className="text-[11px] text-primary mt-1">
                    Extracting concepts, relationships and patterns...
                  </p>
                )}
                {doc.upload_status === 'pending' && (
                  <p className="text-[11px] text-muted-foreground mt-1">Queued for processing...</p>
                )}
                {doc.error_message && (
                  <p className="text-[11px] text-destructive mt-1">{doc.error_message}</p>
                )}
              </div>

              <div className="flex items-center gap-4 flex-shrink-0">
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
          ))}
        </div>
      )}
    </main>
  );
}
