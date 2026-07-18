import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeProps,
  Handle,
  Position,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { api } from '@/api/client';
import type {
  NodeNeighbourhood, GraphNode, GraphEdge,
  Concept, ConsultingPattern,
} from '@/types/api';
import { getFileTypeMeta, fileTypeLabel } from '@/lib/fileTypes';
import {
  X, FileText, Network, ArrowRight, Search, Focus,
  MessageSquare, Zap, GitBranch, RotateCcw, ChevronRight,
  Activity, TrendingUp, ChevronDown, ChevronUp,
} from 'lucide-react';

const TYPE_COLOURS: Record<string, { bg: string; border: string; dot: string; text: string }> = {
  Standard:      { bg: '#eff6ff', border: '#3b82f6', dot: '#3b82f6', text: '#1d4ed8' },
  Technology:    { bg: '#f5f3ff', border: '#8b5cf6', dot: '#8b5cf6', text: '#6d28d9' },
  Methodology:   { bg: '#f0fdf4', border: '#22c55e', dot: '#22c55e', text: '#15803d' },
  Framework:     { bg: '#fff7ed', border: '#f97316', dot: '#f97316', text: '#c2410c' },
  Process:       { bg: '#ecfeff', border: '#06b6d4', dot: '#06b6d4', text: '#0e7490' },
  Capability:    { bg: '#fdf2f8', border: '#ec4899', dot: '#ec4899', text: '#be185d' },
  Regulation:    { bg: '#fff1f2', border: '#ef4444', dot: '#ef4444', text: '#b91c1c' },
  Pattern:       { bg: '#f0fdf4', border: '#16a34a', dot: '#16a34a', text: '#166534' },
  Tool:          { bg: '#faf5ff', border: '#a855f7', dot: '#a855f7', text: '#7e22ce' },
  General:       { bg: '#f9fafb', border: '#9ca3af', dot: '#9ca3af', text: '#4b5563' },
  Requirement:   { bg: '#fefce8', border: '#ca8a04', dot: '#ca8a04', text: '#92400e' },
  Risk:          { bg: '#fff1f2', border: '#f43f5e', dot: '#f43f5e', text: '#9f1239' },
  Issue:         { bg: '#fff7ed', border: '#ea580c', dot: '#ea580c', text: '#9a3412' },
  Dependency:    { bg: '#f0f9ff', border: '#0284c7', dot: '#0284c7', text: '#075985' },
  'Data Element':{ bg: '#f8fafc', border: '#64748b', dot: '#64748b', text: '#334155' },
  Control:       { bg: '#fdf4ff', border: '#c026d3', dot: '#c026d3', text: '#86198f' },
  'Business Rule':{ bg: '#fefce8', border: '#d97706', dot: '#d97706', text: '#78350f' },
  'Business Term':{ bg: '#f0fdf4', border: '#059669', dot: '#059669', text: '#065f46' },
};

function getTypeColour(type?: string | null) {
  return TYPE_COLOURS[type ?? 'General'] ?? TYPE_COLOURS.General;
}

const ALL_CONCEPT_TYPES = Object.keys(TYPE_COLOURS);

function ConceptNode({ data, selected }: NodeProps) {
  const col = getTypeColour(data.type);
  const scale = Math.max(0.85, Math.min(1.35, 1 + (data.importance ?? 0) * 0.35));
  const isFocused = data.focusMode === true;

  return (
    <>
      <Handle type="target" position={Position.Left}
        style={{ background: col.border, border: 'none', width: 6, height: 6 }} />
      <div
        style={{
          background: col.bg,
          border: selected ? `2px solid ${col.border}` : `1.5px solid ${col.border}`,
          borderRadius: 8,
          padding: `${7 * scale}px ${11 * scale}px`,
          minWidth: 90 * scale,
          maxWidth: 170 * scale,
          boxShadow: selected
            ? `0 0 0 3px ${col.border}33, 0 2px 8px rgba(0,0,0,0.12)`
            : '0 1px 3px rgba(0,0,0,0.07)',
          opacity: isFocused ? 0.25 : 1,
          transition: 'opacity 0.2s, box-shadow 0.15s',
          transform: `scale(${scale})`,
          transformOrigin: 'center center',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 2 }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: col.dot, flexShrink: 0 }} />
          <span style={{ fontSize: 11, fontWeight: 600, color: '#111827', lineHeight: 1.3, wordBreak: 'break-word' }}>
            {data.label}
          </span>
        </div>
        {data.type && (
          <span style={{ fontSize: 9, fontWeight: 500, color: col.text, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            {data.type}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Right}
        style={{ background: col.border, border: 'none', width: 6, height: 6 }} />
    </>
  );
}

const NODE_TYPES = { conceptNode: ConceptNode };

function mapToReactFlowNodes(nodes: GraphNode[], importanceMap: Map<string, number>): Node[] {
  return nodes.map(n => ({
    id: n.id,
    position: n.position,
    data: { ...n.data, importance: importanceMap.get(n.id) ?? 0, focusMode: undefined },
    type: 'conceptNode',
  }));
}

function mapToReactFlowEdges(edges: GraphEdge[]): Edge[] {
  return edges.map(e => {
    const strength: number = (e.data as Record<string, unknown>)?.strength as number ?? 0.7;
    const strokeWidth = Math.max(1, Math.round(strength * 3.5));
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.label,
      type: 'smoothstep',
      style: { stroke: '#94a3b8', strokeWidth, opacity: 0.4 + strength * 0.5 },
      labelStyle: { fontSize: 10, fill: '#6b7280' },
      labelBgStyle: { fill: '#fff', fillOpacity: 0.85 },
      labelBgPadding: [3, 5] as [number, number],
      data: e.data,
    };
  });
}

function buildImportanceMap(nodes: GraphNode[], edges: GraphEdge[]): Map<string, number> {
  const degree = new Map<string, number>();
  nodes.forEach(n => degree.set(n.id, 0));
  edges.forEach(e => {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  });
  const max = Math.max(...Array.from(degree.values()), 1);
  const result = new Map<string, number>();
  degree.forEach((v, k) => result.set(k, v / max));
  return result;
}

function GraphHealthBar({
  nodeCount, edgeCount, patternCount, docCount,
  topConnected, topReferenced, onSearch,
}: {
  nodeCount: number; edgeCount: number; patternCount: number; docCount: number;
  topConnected: string; topReferenced: string;
  onSearch: () => void;
}) {
  return (
    <div className="border-b bg-white px-5 py-3 flex items-center gap-6 text-xs flex-wrap">
      <div className="flex items-center gap-1.5 font-semibold text-foreground">
        <Activity className="w-3.5 h-3.5 text-primary" />
        <span>Knowledge Graph</span>
      </div>
      <div className="flex items-center gap-5 text-muted-foreground">
        <span><span className="font-semibold text-foreground">{nodeCount}</span> concepts</span>
        <span><span className="font-semibold text-foreground">{edgeCount}</span> relationships</span>
        <span><span className="font-semibold text-foreground">{patternCount}</span> patterns</span>
        <span><span className="font-semibold text-foreground">{docCount}</span> documents</span>
      </div>
      {topConnected && (
        <div className="flex items-center gap-3 ml-auto text-[11px]">
          <span className="text-muted-foreground">Most connected: <span className="font-medium text-foreground">{topConnected}</span></span>
          {topReferenced && topReferenced !== topConnected && (
            <span className="text-muted-foreground">2nd: <span className="font-medium text-foreground">{topReferenced}</span></span>
          )}
        </div>
      )}
      <button
        onClick={onSearch}
        className="flex items-center gap-1.5 px-3 py-1 border text-[11px] text-muted-foreground hover:text-foreground hover:border-foreground/40 transition-colors ml-2"
      >
        <Search className="w-3 h-3" />
        Search concepts
      </button>
    </div>
  );
}

function SearchOverlay({
  nodes, onSelect, onClose,
}: {
  nodes: Node[];
  onSelect: (nodeId: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);

  const results = useMemo(() => {
    if (!q.trim()) return nodes.slice(0, 12);
    const ql = q.toLowerCase();
    return nodes
      .filter(n =>
        (n.data.label as string).toLowerCase().includes(ql) ||
        (n.data.type as string ?? '').toLowerCase().includes(ql) ||
        (n.data.description as string ?? '').toLowerCase().includes(ql)
      )
      .slice(0, 12);
  }, [q, nodes]);

  return (
    <div className="absolute inset-0 z-50 bg-background/80 flex items-start justify-center pt-16">
      <div className="bg-white border shadow-lg w-full max-w-md mx-4">
        <div className="flex items-center gap-2 px-4 py-3 border-b">
          <Search className="w-4 h-4 text-muted-foreground flex-shrink-0" />
          <input
            ref={inputRef}
            className="flex-1 text-sm focus:outline-none bg-white"
            placeholder="Search concepts, types, descriptions..."
            value={q}
            onChange={e => setQ(e.target.value)}
          />
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="max-h-72 overflow-y-auto">
          {results.length === 0 ? (
            <p className="px-4 py-6 text-sm text-muted-foreground text-center">No concepts match "{q}"</p>
          ) : results.map(n => {
            const col = getTypeColour(n.data.type as string);
            return (
              <button
                key={n.id}
                onClick={() => { onSelect(n.id); onClose(); }}
                className="w-full text-left flex items-center gap-3 px-4 py-2.5 hover:bg-muted/40 transition-colors border-b last:border-0"
              >
                <span style={{ width: 8, height: 8, borderRadius: 2, background: col.bg, border: `1.5px solid ${col.border}`, flexShrink: 0 }} />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-foreground truncate">{n.data.label as string}</p>
                  {n.data.type && <p className="text-[10px] mt-0.5" style={{ color: col.text }}>{n.data.type as string}</p>}
                </div>
                <ChevronRight className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
              </button>
            );
          })}
        </div>
        {!q && nodes.length > 12 && (
          <p className="px-4 py-2 text-[10px] text-muted-foreground border-t">
            Showing 12 of {nodes.length} concepts — type to filter
          </p>
        )}
      </div>
    </div>
  );
}

function PathExplorer({
  nodes, onClose, onHighlight,
}: {
  nodes: Node[];
  onClose: () => void;
  onHighlight: (path: string[]) => void;
}) {
  const [fromId, setFromId] = useState('');
  const [toId, setToId] = useState('');

  const sorted = useMemo(() =>
    [...nodes].sort((a, b) => (a.data.label as string).localeCompare(b.data.label as string)),
  [nodes]);

  const handleFind = () => {
    if (fromId && toId && fromId !== toId) onHighlight([fromId, toId]);
  };

  return (
    <div className="absolute bottom-12 left-3 z-20 bg-white border shadow-sm w-64 p-3 text-xs">
      <div className="flex items-center justify-between mb-2.5">
        <div className="flex items-center gap-1.5 font-semibold text-foreground">
          <GitBranch className="w-3.5 h-3.5 text-primary" />
          Path Explorer
        </div>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <p className="text-[10px] text-muted-foreground mb-2">Find the knowledge path between two concepts.</p>
      <select
        className="w-full border px-2 py-1.5 text-xs mb-2 bg-white focus:outline-none focus:ring-1 focus:ring-foreground"
        value={fromId}
        onChange={e => setFromId(e.target.value)}
      >
        <option value="">From concept...</option>
        {sorted.map(n => <option key={n.id} value={n.id}>{n.data.label as string}</option>)}
      </select>
      <select
        className="w-full border px-2 py-1.5 text-xs mb-3 bg-white focus:outline-none focus:ring-1 focus:ring-foreground"
        value={toId}
        onChange={e => setToId(e.target.value)}
      >
        <option value="">To concept...</option>
        {sorted.map(n => <option key={n.id} value={n.id}>{n.data.label as string}</option>)}
      </select>
      <button
        onClick={handleFind}
        disabled={!fromId || !toId || fromId === toId}
        className="w-full px-3 py-1.5 bg-foreground text-background text-xs font-medium disabled:opacity-40 hover:opacity-80 transition-opacity"
      >
        Highlight path
      </button>
    </div>
  );
}

function EdgeInfoPanel({
  edge, nodeMap, onClose, onAskAssistant,
}: {
  edge: Edge;
  nodeMap: Map<string, Node>;
  onClose: () => void;
  onAskAssistant: (q: string) => void;
}) {
  const src = nodeMap.get(edge.source);
  const tgt = nodeMap.get(edge.target);
  const edgeData = edge.data as Record<string, unknown>;
  const strength: number = edgeData?.strength as number ?? 0.7;
  const relType = edgeData?.relationship_type as string ?? edge.label ?? 'related_to';
  const reasoning = edgeData?.reasoning as string | null ?? null;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-5 py-3.5 border-b">
        <div className="flex items-center gap-2">
          <ArrowRight className="w-4 h-4 text-muted-foreground" />
          <span className="text-sm font-semibold text-foreground">Relationship</span>
        </div>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground p-0.5">
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="px-5 py-4 border-b">
        <div className="flex items-center gap-2 text-sm font-medium text-foreground flex-wrap">
          <span style={{ color: getTypeColour(src?.data.type as string).text }}>{src?.data.label as string}</span>
          <span className="font-mono text-[11px] px-2 py-0.5 bg-muted text-muted-foreground border rounded">
            {relType.replace(/_/g, ' ')}
          </span>
          <span style={{ color: getTypeColour(tgt?.data.type as string).text }}>{tgt?.data.label as string}</span>
        </div>
      </div>
      <div className="px-5 py-4 border-b">
        <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Relationship strength</p>
        <div className="flex items-center gap-3">
          <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden">
            <div className="h-full bg-primary rounded-full" style={{ width: `${Math.round(strength * 100)}%` }} />
          </div>
          <span className="text-xs font-semibold text-foreground">{Math.round(strength * 100)}%</span>
        </div>
        <p className="text-[10px] text-muted-foreground mt-1.5">
          {strength >= 0.85 ? 'Explicitly stated, central to source document.' :
           strength >= 0.7  ? 'Clearly stated in the source document.' :
                              'Implied or briefly mentioned.'}
        </p>
      </div>
      {reasoning && (
        <div className="px-5 py-4 border-b">
          <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Evidence basis</p>
          <p className="text-xs text-foreground/80 leading-relaxed italic">"{reasoning}"</p>
        </div>
      )}
      <div className="px-5 py-4">
        <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Ask the assistant</p>
        <button
          onClick={() => onAskAssistant(`Why is "${src?.data.label}" ${relType.replace(/_/g, ' ')} "${tgt?.data.label}"? Show supporting evidence.`)}
          className="w-full text-left text-xs px-3 py-2 border rounded-sm hover:bg-muted/40 transition-colors flex items-center justify-between group"
        >
          <span>Explain this relationship with evidence</span>
          <MessageSquare className="w-3 h-3 text-muted-foreground opacity-0 group-hover:opacity-100" />
        </button>
      </div>
    </div>
  );
}

function ConceptDetailPanel({
  selected, patterns, onClose, onAskAssistant, onNavigateToNode,
}: {
  selected: NodeNeighbourhood;
  patterns: ConsultingPattern[];
  onClose: () => void;
  onAskAssistant: (q: string) => void;
  onNavigateToNode: (nodeId: number) => void;
}) {
  const col = getTypeColour(selected.node.type);
  const relatedPatterns = patterns.filter(p =>
    p.related_concept_ids.includes(selected.node.id)
  );

  const allSourceDocs = (() => {
    const docs = new Map<number, { title: string; file_type: string }>();
    if (selected.source_document) {
      docs.set(selected.source_document.id, {
        title: selected.source_document.title || selected.source_document.filename.split('/').pop() || '',
        file_type: selected.source_document.file_type,
      });
    }
    selected.neighbours.forEach(n => {
      if (n.source_document_id && !docs.has(n.source_document_id)) {
        docs.set(n.source_document_id, {
          title: `Document #${n.source_document_id}`,
          file_type: '',
        });
      }
    });
    return Array.from(docs.values());
  })();

  const docTypeCounts = (() => {
    const counts: Record<string, number> = {};
    if (selected.source_document?.file_type) {
      const ft = selected.source_document.file_type;
      counts[ft] = (counts[ft] ?? 0) + 1;
    }
    return counts;
  })();

  const suggestedQuestions = [
    `Explain ${selected.node.name} based on the documents in this workspace`,
    `What risks or challenges are associated with ${selected.node.name}?`,
    `Show supporting evidence for ${selected.node.name}`,
    `How does ${selected.node.name} connect to other concepts in this workspace?`,
  ];

  return (
    <div className="flex flex-col h-full overflow-y-auto">
      <div className="flex items-center justify-between px-5 py-3.5 border-b flex-shrink-0">
        <div className="flex items-center gap-2">
          <Network className="w-4 h-4 text-muted-foreground" />
          <span className="text-sm font-semibold text-foreground">Concept Detail</span>
        </div>
        <button onClick={onClose} className="text-muted-foreground hover:text-foreground p-0.5">
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="px-5 pt-5 pb-4 border-b">
        <div className="flex items-start gap-3 mb-2">
          <span style={{ width: 10, height: 10, borderRadius: 3, background: col.bg, border: `2px solid ${col.border}`, flexShrink: 0, marginTop: 4 }} />
          <h2 className="font-bold text-[17px] text-foreground leading-tight">{selected.node.name}</h2>
        </div>
        {selected.node.type && (
          <span className={`type-pill type-${selected.node.type.replace(/\s+/g, '-')}`}
            style={{ background: col.bg, color: col.text, borderColor: `${col.border}40` }}>
            {selected.node.type}
          </span>
        )}
        <div className="flex items-center gap-3 mt-2.5 text-[10px] text-muted-foreground">
          <span>{selected.neighbours.length} connections</span>
          <span>{allSourceDocs.length} source {allSourceDocs.length === 1 ? 'doc' : 'docs'}</span>
          {relatedPatterns.length > 0 && <span>{relatedPatterns.length} pattern{relatedPatterns.length > 1 ? 's' : ''}</span>}
        </div>
      </div>

      {selected.node.description && (
        <div className="px-5 py-4 border-b">
          <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">What is this?</p>
          <p className="text-sm text-foreground leading-relaxed">{selected.node.description}</p>
        </div>
      )}

      {selected.node.source_excerpt && (
        <div className="px-5 py-4 border-b">
          <div className="flex items-center gap-1.5 mb-2">
            <FileText className="w-3.5 h-3.5 text-muted-foreground" />
            <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">Source evidence</p>
          </div>
          <blockquote className="text-xs text-foreground italic leading-relaxed border-l-2 pl-3 py-0.5"
            style={{ borderColor: col.border }}>
            "{selected.node.source_excerpt}"
          </blockquote>
          {selected.source_document && (
            <div className="mt-2 flex items-center gap-2">
              <span
                className="text-[10px] font-bold px-1.5 py-0.5 rounded flex-shrink-0"
                style={(() => { const m = getFileTypeMeta(selected.source_document.file_type); return { background: m.bg, border: `1px solid ${m.border}`, color: m.text }; })()}
              >
                {fileTypeLabel(selected.source_document.file_type)}
              </span>
              <p className="text-[11px] font-medium text-foreground truncate">
                {selected.source_document.title || selected.source_document.filename.split('/').pop()}
              </p>
            </div>
          )}
        </div>
      )}

      {allSourceDocs.length > 0 && (
        <div className="px-5 py-4 border-b">
          <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Supported by</p>
          <div className="space-y-1.5">
            {allSourceDocs.map((doc, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                {doc.file_type && (
                  <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded flex-shrink-0 bg-muted border text-muted-foreground">
                    {fileTypeLabel(doc.file_type)}
                  </span>
                )}
                <span className="truncate text-foreground">{doc.title}</span>
              </div>
            ))}
          </div>
          {Object.keys(docTypeCounts).length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {Object.entries(docTypeCounts).map(([ft, count]) => (
                <span key={ft} className="text-[10px] text-muted-foreground bg-muted px-2 py-0.5 rounded border">
                  {count} {fileTypeLabel(ft)} {count === 1 ? 'file' : 'files'}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="px-5 py-4 border-b">
        <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Ask the assistant</p>
        <div className="grid grid-cols-2 gap-1.5">
          {suggestedQuestions.slice(0, 2).map((q, i) => (
            <button
              key={i}
              onClick={() => onAskAssistant(q)}
              className="text-left text-[11px] px-2.5 py-2 border rounded-sm hover:bg-muted/40 transition-colors flex items-start gap-1.5 leading-snug"
            >
              <MessageSquare className="w-3 h-3 text-primary mt-0.5 flex-shrink-0" />
              <span>{q.length > 50 ? q.slice(0, 50) + '…' : q}</span>
            </button>
          ))}
          {suggestedQuestions.slice(2).map((q, i) => (
            <button
              key={i + 2}
              onClick={() => onAskAssistant(q)}
              className="text-left text-[11px] px-2.5 py-2 border rounded-sm hover:bg-muted/40 transition-colors flex items-start gap-1.5 leading-snug"
            >
              <Zap className="w-3 h-3 text-primary mt-0.5 flex-shrink-0" />
              <span>{q.length > 50 ? q.slice(0, 50) + '…' : q}</span>
            </button>
          ))}
        </div>
      </div>

      {selected.neighbours.length > 0 && (
        <div className="px-5 py-4 border-b">
          <div className="flex items-center gap-1.5 mb-3">
            <Network className="w-3.5 h-3.5 text-muted-foreground" />
            <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">
              Connected ({selected.neighbours.length})
            </p>
          </div>
          <div className="space-y-1.5">
            {selected.neighbours.map(n => {
              const edge = selected.edges.find(
                e => (e.source_concept_id === selected.node.id && e.target_concept_id === n.id) ||
                     (e.target_concept_id === selected.node.id && e.source_concept_id === n.id)
              );
              const nCol = getTypeColour(n.type);
              return (
                <button
                  key={n.id}
                  onClick={() => onNavigateToNode(n.id)}
                  className="w-full text-left flex items-start gap-2.5 p-2 border rounded-sm hover:bg-muted/30 transition-colors group"
                >
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: nCol.dot, flexShrink: 0, marginTop: 4 }} />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-medium text-foreground leading-tight">{n.name}</p>
                    {n.type && <p className="text-[10px] mt-0.5" style={{ color: nCol.text }}>{n.type}</p>}
                    {edge?.reasoning && (
                      <p className="text-[10px] text-muted-foreground mt-0.5 italic line-clamp-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        {edge.reasoning}
                      </p>
                    )}
                  </div>
                  {edge && (
                    <div className="flex flex-col items-end gap-0.5 flex-shrink-0">
                      <span className="text-[10px] bg-muted text-muted-foreground px-1.5 py-0.5 rounded font-mono opacity-70 group-hover:opacity-100">
                        {edge.relationship_type.replace(/_/g, ' ')}
                      </span>
                      {edge.strength !== undefined && (
                        <span className="text-[9px] text-muted-foreground/60 tabular-nums">
                          {Math.round(edge.strength * 100)}%
                        </span>
                      )}
                    </div>
                  )}
                  <ChevronRight className="w-3 h-3 text-muted-foreground flex-shrink-0 mt-0.5 opacity-0 group-hover:opacity-100" />
                </button>
              );
            })}
          </div>
        </div>
      )}

      {relatedPatterns.length > 0 && (
        <div className="px-5 py-4 border-b">
          <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Related patterns</p>
          <div className="space-y-1.5">
            {relatedPatterns.map(p => (
              <button
                key={p.id}
                onClick={() => onAskAssistant(`Explain the "${p.name}" pattern and how it applies to ${selected.node.name}`)}
                className="w-full text-left p-2 border rounded-sm hover:bg-muted/30 transition-colors"
              >
                <p className="text-xs font-medium text-foreground">{p.name}</p>
                {p.problem_statement && (
                  <p className="text-[10px] text-muted-foreground mt-0.5 line-clamp-2">{p.problem_statement}</p>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="px-5 py-4">
        <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Discover more</p>
        <div className="space-y-1.5">
          <button
            onClick={() => onAskAssistant(`What concepts are most strongly connected to ${selected.node.name} in this workspace?`)}
            className="w-full text-left text-[11px] px-2.5 py-2 border rounded-sm hover:bg-muted/40 transition-colors flex items-center gap-1.5"
          >
            <Activity className="w-3 h-3 text-muted-foreground" />
            Find strongly connected concepts
          </button>
          <button
            onClick={() => onAskAssistant(`What documents mention ${selected.node.name} and what do they say about it?`)}
            className="w-full text-left text-[11px] px-2.5 py-2 border rounded-sm hover:bg-muted/40 transition-colors flex items-center gap-1.5"
          >
            <FileText className="w-3 h-3 text-muted-foreground" />
            Show all source documents
          </button>
        </div>
      </div>
    </div>
  );
}

function PatternIntelligencePanel({
  patterns, rawNodes, rawEdges, onAskAssistant,
}: {
  patterns: ConsultingPattern[];
  rawNodes: GraphNode[];
  rawEdges: GraphEdge[];
  onAskAssistant: (q: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [expandedPattern, setExpandedPattern] = useState<number | null>(null);

  const degree = useMemo(() => {
    const d = new Map<string, number>();
    rawNodes.forEach(n => d.set(n.id, 0));
    rawEdges.forEach(e => {
      d.set(e.source, (d.get(e.source) ?? 0) + 1);
      d.set(e.target, (d.get(e.target) ?? 0) + 1);
    });
    return d;
  }, [rawNodes, rawEdges]);

  const topNodes = useMemo(() =>
    rawNodes.slice()
      .sort((a, b) => (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0))
      .slice(0, 6),
  [rawNodes, degree]);

  if (patterns.length === 0 && rawNodes.length === 0) return null;

  return (
    <aside className="w-[240px] border-r bg-white flex flex-col flex-shrink-0 overflow-y-auto text-xs">
      <button
        onClick={() => setExpanded(v => !v)}
        className="flex items-center justify-between px-4 py-3 border-b hover:bg-muted/30 transition-colors flex-shrink-0 w-full text-left"
      >
        <div className="flex items-center gap-1.5">
          <TrendingUp className="w-3.5 h-3.5 text-primary" />
          <span className="font-semibold text-foreground text-[11px] uppercase tracking-wider">Pattern Intelligence</span>
        </div>
        {expanded ? <ChevronUp className="w-3 h-3 text-muted-foreground" /> : <ChevronDown className="w-3 h-3 text-muted-foreground" />}
      </button>

      {expanded && (
        <>
          {/* Patterns */}
          {patterns.length > 0 && (
            <div className="px-3 pt-3 pb-2 border-b">
              <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                {patterns.length} Consulting Pattern{patterns.length > 1 ? 's' : ''} Detected
              </p>
              <div className="space-y-1.5">
                {patterns.map(p => (
                  <div key={p.id} className="border rounded-sm overflow-hidden">
                    <button
                      onClick={() => setExpandedPattern(expandedPattern === p.id ? null : p.id)}
                      className="w-full text-left px-2.5 py-2 hover:bg-muted/30 transition-colors flex items-start justify-between gap-1"
                    >
                      <div className="min-w-0 flex-1">
                        <p className="font-semibold text-foreground leading-tight text-[11px]">{p.name}</p>
                        {p.problem_statement && (
                          <p className="text-[10px] text-muted-foreground mt-0.5 leading-snug line-clamp-2">
                            {p.problem_statement}
                          </p>
                        )}
                      </div>
                      {expandedPattern === p.id
                        ? <ChevronUp className="w-3 h-3 text-muted-foreground flex-shrink-0 mt-0.5" />
                        : <ChevronDown className="w-3 h-3 text-muted-foreground flex-shrink-0 mt-0.5" />
                      }
                    </button>
                    {expandedPattern === p.id && (
                      <div className="px-2.5 pb-2.5 border-t bg-muted/20">
                        {p.ibm_approach.length > 0 && (
                          <div className="mt-2">
                            <p className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-1">Approach</p>
                            <ol className="space-y-0.5">
                              {p.ibm_approach.slice(0, 4).map((step, i) => (
                                <li key={i} className="text-[10px] text-foreground flex gap-1.5">
                                  <span className="text-muted-foreground flex-shrink-0">{i + 1}.</span>
                                  <span>{step}</span>
                                </li>
                              ))}
                            </ol>
                          </div>
                        )}
                        <button
                          onClick={() => onAskAssistant(`Explain the "${p.name}" pattern — what problem does it solve and how is it applied?`)}
                          className="mt-2 w-full text-left text-[10px] px-2 py-1.5 border rounded-sm hover:bg-white transition-colors flex items-center gap-1.5 text-primary"
                        >
                          <MessageSquare className="w-3 h-3 flex-shrink-0" />
                          Ask assistant about this pattern
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Top concepts by connection degree */}
          {topNodes.length > 0 && (
            <div className="px-3 pt-3 pb-2">
              <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                Most Connected Concepts
              </p>
              <div className="space-y-1">
                {topNodes.map((n, i) => {
                  const col = getTypeColour(n.data.type);
                  const deg = degree.get(n.id) ?? 0;
                  const maxDeg = degree.get(topNodes[0].id) ?? 1;
                  return (
                    <div key={n.id} className="flex items-center gap-2">
                      <span className="text-[9px] text-muted-foreground w-3 flex-shrink-0 text-right">{i + 1}</span>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1 mb-0.5">
                          <span style={{ width: 6, height: 6, borderRadius: 1, background: col.bg, border: `1.5px solid ${col.border}`, flexShrink: 0 }} />
                          <span className="text-[11px] font-medium text-foreground truncate">{n.data.label as string}</span>
                        </div>
                        <div className="h-0.5 w-full bg-muted rounded-full overflow-hidden">
                          <div
                            className="h-full bg-primary rounded-full"
                            style={{ width: `${Math.round((deg / maxDeg) * 100)}%` }}
                          />
                        </div>
                      </div>
                      <span className="text-[9px] text-muted-foreground flex-shrink-0">{deg}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </>
      )}
    </aside>
  );
}

export default function GraphExplorer() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);
  const navigate = useNavigate();

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [allNodes, setAllNodes] = useState<Node[]>([]);
  const [allEdges, setAllEdges] = useState<Edge[]>([]);
  const [rawNodes, setRawNodes] = useState<GraphNode[]>([]);
  const [rawEdges, setRawEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(true);

  const [nodeCount, setNodeCount] = useState(0);
  const [edgeCount, setEdgeCount] = useState(0);
  const [patternCount, setPatternCount] = useState(0);
  const [docCount, setDocCount] = useState(0);
  const [allPatterns, setAllPatterns] = useState<ConsultingPattern[]>([]);

  const [selected, setSelected] = useState<NodeNeighbourhood | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<Edge | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [activeTypeFilters, setActiveTypeFilters] = useState<Set<string>>(new Set());
  const [showSearch, setShowSearch] = useState(false);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null);
  const [showPathExplorer, setShowPathExplorer] = useState(false);
  const [pathHighlight, setPathHighlight] = useState<Set<string>>(new Set());

  const nodeMapRef = useRef<Map<string, Node>>(new Map());

  const { topConnected, topReferenced } = useMemo(() => {
    if (!rawNodes.length) return { topConnected: '', topReferenced: '' };
    const degree = new Map<string, number>();
    rawNodes.forEach(n => degree.set(n.id, 0));
    rawEdges.forEach(e => {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    });
    const byDegree = rawNodes.slice().sort((a, b) => (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0));
    return {
      topConnected: byDegree[0]?.data.label ?? '',
      topReferenced: byDegree[1]?.data.label ?? '',
    };
  }, [rawNodes, rawEdges]);

  const loadGraph = useCallback(async () => {
    setLoading(true);
    try {
      const [graphData, patterns, docs] = await Promise.all([
        api.knowledge.graph(wsId),
        api.knowledge.patterns(wsId).catch(() => [] as ConsultingPattern[]),
        api.documents.list(wsId).catch(() => []),
      ]);

      const importanceMap = buildImportanceMap(graphData.nodes, graphData.edges);
      const rfNodes = mapToReactFlowNodes(graphData.nodes, importanceMap);
      const rfEdges = mapToReactFlowEdges(graphData.edges);

      setRawNodes(graphData.nodes);
      setRawEdges(graphData.edges);
      setAllNodes(rfNodes);
      setAllEdges(rfEdges);
      setNodes(rfNodes);
      setEdges(rfEdges);
      setNodeCount(graphData.nodes.length);
      setEdgeCount(graphData.edges.length);
      setAllPatterns(patterns);
      setPatternCount(patterns.length);
      setDocCount(docs.filter(d => d.upload_status === 'complete').length);

      const map = new Map<string, Node>();
      rfNodes.forEach(n => map.set(n.id, n));
      nodeMapRef.current = map;
    } finally {
      setLoading(false);
    }
  }, [wsId]);

  useEffect(() => { loadGraph(); }, [loadGraph]);

  useEffect(() => {
    let visibleNodes = allNodes;
    let visibleEdges = allEdges;

    if (activeTypeFilters.size > 0) {
      visibleNodes = allNodes.filter(n =>
        activeTypeFilters.has((n.data as { type?: string }).type ?? 'General')
      );
    }

    if (focusNodeId) {
      const focusNeighbours = new Set<string>([focusNodeId]);
      allEdges.forEach(e => {
        if (e.source === focusNodeId || e.target === focusNodeId) {
          focusNeighbours.add(e.source);
          focusNeighbours.add(e.target);
        }
      });
      const firstHop = new Set(focusNeighbours);
      allEdges.forEach(e => {
        if (firstHop.has(e.source) || firstHop.has(e.target)) {
          focusNeighbours.add(e.source);
          focusNeighbours.add(e.target);
        }
      });
      visibleNodes = visibleNodes.map(n => ({
        ...n,
        data: { ...n.data, focusMode: !focusNeighbours.has(n.id) },
      }));
    }

    if (pathHighlight.size > 0) {
      visibleEdges = allEdges.map(e => ({
        ...e,
        style: {
          ...e.style,
          stroke: pathHighlight.has(e.source) && pathHighlight.has(e.target) ? '#3b82f6' : '#e2e8f0',
          strokeWidth: pathHighlight.has(e.source) && pathHighlight.has(e.target) ? 3 : 1,
          opacity: pathHighlight.has(e.source) && pathHighlight.has(e.target) ? 1 : 0.2,
        },
      }));
    }

    const nodeIds = new Set(visibleNodes.map(n => n.id));
    setNodes(visibleNodes);
    setEdges(visibleEdges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target)));
  }, [activeTypeFilters, focusNodeId, pathHighlight, allNodes, allEdges]);

  const handleNodeClick = useCallback(async (_: React.MouseEvent, node: Node) => {
    setSelectedEdge(null);
    setDetailLoading(true);
    setSelected(null);
    setFocusNodeId(null);
    try {
      const detail = await api.knowledge.nodeNeighbourhood(wsId, Number(node.id));
      setSelected(detail);
    } finally {
      setDetailLoading(false);
    }
  }, [wsId]);

  const handleEdgeClick = useCallback((_: React.MouseEvent, edge: Edge) => {
    setSelected(null);
    setSelectedEdge(edge);
  }, []);

  const handleSearchSelect = useCallback((nodeId: string) => {
    const node = nodeMapRef.current.get(nodeId);
    if (!node) return;
    setFocusNodeId(nodeId);
    handleNodeClick(new MouseEvent('click') as unknown as React.MouseEvent, node);
  }, [handleNodeClick]);

  const handleFocusToggle = useCallback(() => {
    if (!selected) return;
    const nid = String(selected.node.id);
    setFocusNodeId(prev => prev === nid ? null : nid);
  }, [selected]);

  const handleNavigateToNode = useCallback(async (nodeId: number) => {
    setDetailLoading(true);
    setSelected(null);
    try {
      const detail = await api.knowledge.nodeNeighbourhood(wsId, nodeId);
      setSelected(detail);
      setFocusNodeId(String(nodeId));
    } finally {
      setDetailLoading(false);
    }
  }, [wsId]);

  const handleAskAssistant = useCallback((question: string, conceptName?: string) => {
    navigate(`/workspace/${wsId}/chat`, {
      state: { prefill: question, conceptName: conceptName ?? selected?.node.name ?? undefined },
    });
  }, [navigate, wsId, selected]);

  const handlePathHighlight = useCallback((nodeIds: string[]) => {
    setPathHighlight(new Set(nodeIds));
    setShowPathExplorer(false);
  }, []);

  const clearFocus = useCallback(() => {
    setFocusNodeId(null);
    setPathHighlight(new Set());
  }, []);

  const toggleTypeFilter = useCallback((type: string) => {
    setActiveTypeFilters(prev => {
      const next = new Set(prev);
      if (next.has(type)) { next.delete(type); } else { next.add(type); }
      return next;
    });
  }, []);

  const clearFilters = useCallback(() => setActiveTypeFilters(new Set()), []);

  const panelOpen = !!(selected || detailLoading || selectedEdge);

  return (
    <div className="flex flex-col h-[calc(100vh-112px)]">

      {!loading && nodeCount > 0 && (
        <GraphHealthBar
          nodeCount={nodeCount}
          edgeCount={edgeCount}
          patternCount={patternCount}
          docCount={docCount}
          topConnected={topConnected}
          topReferenced={topReferenced}
          onSearch={() => setShowSearch(true)}
        />
      )}

      <div className="flex flex-1 min-h-0">

        {!loading && (nodeCount > 0 || allPatterns.length > 0) && (
          <PatternIntelligencePanel
            patterns={allPatterns}
            rawNodes={rawNodes}
            rawEdges={rawEdges}
            onAskAssistant={handleAskAssistant}
          />
        )}

        <div className="flex-1 relative">

          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-background/70 z-10 text-sm text-muted-foreground">
              Building knowledge graph...
            </div>
          )}

          {!loading && nodeCount === 0 && (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-center px-8">
              <Network className="w-10 h-10 text-muted-foreground mb-4" />
              <p className="text-sm font-semibold text-foreground mb-1">No knowledge graph yet</p>
              <p className="text-xs text-muted-foreground max-w-xs">
                Upload and process documents in the Documents tab. The graph is built automatically once extraction completes.
              </p>
            </div>
          )}

          {showSearch && (
            <SearchOverlay
              nodes={allNodes}
              onSelect={handleSearchSelect}
              onClose={() => setShowSearch(false)}
            />
          )}

          {(focusNodeId || pathHighlight.size > 0) && (
            <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 flex items-center gap-2 bg-white border shadow-sm px-3 py-1.5 text-xs">
              {focusNodeId && (
                <span className="text-muted-foreground flex items-center gap-1.5">
                  <Focus className="w-3 h-3 text-primary" />
                  Focus mode — showing 2-hop neighbourhood
                </span>
              )}
              {pathHighlight.size > 0 && (
                <span className="text-muted-foreground flex items-center gap-1.5">
                  <GitBranch className="w-3 h-3 text-primary" />
                  Path highlighted
                </span>
              )}
              <button onClick={clearFocus} className="flex items-center gap-1 text-primary hover:underline ml-1">
                <RotateCcw className="w-3 h-3" />
                Clear
              </button>
            </div>
          )}

          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeClick={handleNodeClick}
            onEdgeClick={handleEdgeClick}
            nodeTypes={NODE_TYPES}
            fitView
            fitViewOptions={{ padding: 0.25 }}
            minZoom={0.1}
            maxZoom={2.5}
          >
            <Background color="#f1f5f9" gap={20} />
            <Controls showInteractive={false} />
            <MiniMap
              nodeColor={n => getTypeColour((n.data as { type?: string }).type).border}
              maskColor="rgba(255,255,255,0.75)"
            />
          </ReactFlow>

          {nodeCount > 0 && !panelOpen && (
            <div className="absolute bottom-3 left-3 z-10 bg-white border rounded shadow-sm px-3 py-2 text-[10px]">
              <p className="font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Concept types</p>
              <div className="flex flex-col gap-1">
                {Object.entries(TYPE_COLOURS).slice(0, 8).map(([type, col]) => (
                  <div key={type} className="flex items-center gap-1.5">
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: col.bg, border: `1.5px solid ${col.border}`, flexShrink: 0 }} />
                    <span style={{ color: col.text }}>{type}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {nodeCount > 1 && (
            <div className="absolute bottom-3 right-3 z-10 flex flex-col gap-1.5 items-end">
              {!showPathExplorer && (
                <button
                  onClick={() => setShowPathExplorer(true)}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 bg-white border shadow-sm text-[11px] text-muted-foreground hover:text-foreground transition-colors"
                >
                  <GitBranch className="w-3 h-3" />
                  Path explorer
                </button>
              )}
            </div>
          )}

          {showPathExplorer && (
            <PathExplorer
              nodes={allNodes}
              onClose={() => setShowPathExplorer(false)}
              onHighlight={handlePathHighlight}
            />
          )}

          {nodeCount > 0 && (
            <div className="absolute top-3 right-3 z-10 bg-white border rounded shadow-sm px-3 py-2 text-[10px] max-w-[230px]">
              <div className="flex items-center justify-between mb-1.5">
                <p className="font-semibold text-muted-foreground uppercase tracking-wider">Filter by type</p>
                {activeTypeFilters.size > 0 && (
                  <button onClick={clearFilters} className="text-[10px] text-primary hover:underline ml-2">Clear</button>
                )}
              </div>
              <div className="flex flex-wrap gap-1">
                {ALL_CONCEPT_TYPES.map(type => {
                  const c = getTypeColour(type);
                  const active = activeTypeFilters.has(type);
                  return (
                    <button
                      key={type}
                      onClick={() => toggleTypeFilter(type)}
                      style={{
                        background: active ? c.bg : '#f9fafb',
                        border: `1px solid ${active ? c.border : '#e5e7eb'}`,
                        color: active ? c.text : '#6b7280',
                        borderRadius: 3,
                        padding: '1px 6px',
                        fontSize: 10,
                        fontWeight: active ? 600 : 400,
                        cursor: 'pointer',
                      }}
                    >
                      {type}
                    </button>
                  );
                })}
              </div>
              {activeTypeFilters.size > 0 && (
                <p className="text-[10px] text-muted-foreground mt-1.5">
                  Showing {nodes.length} of {nodeCount} concepts
                </p>
              )}
            </div>
          )}
        </div>

        {panelOpen && (
          <aside className="w-[360px] border-l bg-white overflow-y-auto flex flex-col flex-shrink-0">
            {detailLoading && (
              <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">Loading...</div>
            )}

            {!detailLoading && selectedEdge && (
              <EdgeInfoPanel
                edge={selectedEdge}
                nodeMap={nodeMapRef.current}
                onClose={() => setSelectedEdge(null)}
                onAskAssistant={handleAskAssistant}
              />
            )}

            {!detailLoading && selected && !selectedEdge && (
              <>
                <div className="flex items-center gap-2 px-5 pt-3 pb-0">
                  <button
                    onClick={handleFocusToggle}
                    className={`flex items-center gap-1.5 text-[11px] px-2.5 py-1 border rounded-sm transition-colors ${
                      focusNodeId === String(selected.node.id)
                        ? 'bg-foreground text-background border-foreground'
                        : 'text-muted-foreground hover:text-foreground border-border'
                    }`}
                  >
                    <Focus className="w-3 h-3" />
                    {focusNodeId === String(selected.node.id) ? 'Exit focus' : 'Focus mode'}
                  </button>
                  <button
                    onClick={() => handleAskAssistant(`Tell me everything about ${selected.node.name} based on the documents in this workspace.`)}
                    className="flex items-center gap-1.5 text-[11px] px-2.5 py-1 border rounded-sm text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <MessageSquare className="w-3 h-3" />
                    Ask assistant
                  </button>
                </div>
                <ConceptDetailPanel
                  selected={selected}
                  patterns={allPatterns}
                  onClose={() => { setSelected(null); setFocusNodeId(null); }}
                  onAskAssistant={handleAskAssistant}
                  onNavigateToNode={handleNavigateToNode}
                />
              </>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}
