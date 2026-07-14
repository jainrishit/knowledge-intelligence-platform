import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
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
import type { NodeNeighbourhood, GraphNode, GraphEdge } from '@/types/api';
import { X, FileText, Network, Info, ArrowRight } from 'lucide-react';

const TYPE_COLOURS: Record<string, { bg: string; border: string; dot: string; text: string }> = {
  Standard:    { bg: '#eff6ff', border: '#3b82f6', dot: '#3b82f6', text: '#1d4ed8' },
  Technology:  { bg: '#f5f3ff', border: '#8b5cf6', dot: '#8b5cf6', text: '#6d28d9' },
  Methodology: { bg: '#f0fdf4', border: '#22c55e', dot: '#22c55e', text: '#15803d' },
  Framework:   { bg: '#fff7ed', border: '#f97316', dot: '#f97316', text: '#c2410c' },
  Process:     { bg: '#ecfeff', border: '#06b6d4', dot: '#06b6d4', text: '#0e7490' },
  Capability:  { bg: '#fdf2f8', border: '#ec4899', dot: '#ec4899', text: '#be185d' },
  Regulation:  { bg: '#fff1f2', border: '#ef4444', dot: '#ef4444', text: '#b91c1c' },
  Pattern:     { bg: '#f0fdf4', border: '#16a34a', dot: '#16a34a', text: '#166534' },
  Tool:        { bg: '#faf5ff', border: '#a855f7', dot: '#a855f7', text: '#7e22ce' },
  General:     { bg: '#f9fafb', border: '#9ca3af', dot: '#9ca3af', text: '#4b5563' },
};

function getTypeColour(type?: string | null) {
  return TYPE_COLOURS[type ?? 'General'] ?? TYPE_COLOURS.General;
}

function ConceptNode({ data }: NodeProps) {
  const col = getTypeColour(data.type);
  return (
    <>
      <Handle type="target" position={Position.Left} style={{ background: col.border, border: 'none', width: 6, height: 6 }} />
      <div
        style={{
          background: col.bg,
          border: `1.5px solid ${col.border}`,
          borderRadius: 8,
          padding: '7px 11px',
          minWidth: 100,
          maxWidth: 160,
          boxShadow: '0 1px 3px rgba(0,0,0,0.07)',
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
      <Handle type="source" position={Position.Right} style={{ background: col.border, border: 'none', width: 6, height: 6 }} />
    </>
  );
}

const NODE_TYPES = { conceptNode: ConceptNode };

function mapToReactFlowNodes(nodes: GraphNode[]): Node[] {
  return nodes.map(n => ({
    id: n.id,
    position: n.position,
    data: n.data,
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
    };
  });
}

const LEGEND_ITEMS = Object.entries(TYPE_COLOURS).slice(0, 7);

function Legend() {
  return (
    <div className="absolute bottom-3 left-3 z-10 bg-white border rounded shadow-sm px-3 py-2 text-[10px]">
      <p className="font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Concept types</p>
      <div className="flex flex-col gap-1">
        {LEGEND_ITEMS.map(([type, col]) => (
          <div key={type} className="flex items-center gap-1.5">
            <span style={{ width: 8, height: 8, borderRadius: 2, background: col.bg, border: `1.5px solid ${col.border}`, flexShrink: 0 }} />
            <span style={{ color: col.text }}>{type}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function GraphExplorer() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<NodeNeighbourhood | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [nodeCount, setNodeCount] = useState(0);
  const [edgeCount, setEdgeCount] = useState(0);

  const loadGraph = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.knowledge.graph(wsId);
      setNodes(mapToReactFlowNodes(data.nodes));
      setEdges(mapToReactFlowEdges(data.edges));
      setNodeCount(data.nodes.length);
      setEdgeCount(data.edges.length);
    } finally {
      setLoading(false);
    }
  }, [wsId]);

  useEffect(() => { loadGraph(); }, [loadGraph]);

  const handleNodeClick = async (_: React.MouseEvent, node: Node) => {
    setDetailLoading(true);
    setSelected(null);
    try {
      const detail = await api.knowledge.nodeNeighbourhood(wsId, Number(node.id));
      setSelected(detail);
    } finally {
      setDetailLoading(false);
    }
  };

  const col = selected ? getTypeColour(selected.node.type) : null;

  return (
    <div className="flex h-[calc(100vh-140px)]">

      {/* Graph canvas */}
      <div className="flex-1 relative">
        {/* Top info bar */}
        {!loading && nodeCount > 0 && (
          <div className="absolute top-3 left-3 z-10 flex items-center gap-3 bg-white border rounded px-3 py-1.5 text-xs text-muted-foreground shadow-sm">
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-primary" />
              {nodeCount} concepts
            </span>
            <span className="flex items-center gap-1">
              <ArrowRight className="w-3 h-3" />
              {edgeCount} relationships
            </span>
            <span className="text-[10px] text-muted-foreground">Click any node to explore</span>
          </div>
        )}

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

        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={handleNodeClick}
          nodeTypes={NODE_TYPES}
          fitView
          fitViewOptions={{ padding: 0.25 }}
          minZoom={0.15}
          maxZoom={2.5}
        >
          <Background color="#f1f5f9" gap={20} />
          <Controls showInteractive={false} />
          <MiniMap
            nodeColor={n => getTypeColour((n.data as { type?: string }).type).border}
            maskColor="rgba(255,255,255,0.75)"
          />
        </ReactFlow>

        {nodeCount > 0 && <Legend />}
      </div>

      {/* Side panel */}
      {(selected || detailLoading) && (
        <aside className="w-[340px] border-l bg-white overflow-y-auto flex flex-col">
          {/* Panel header */}
          <div className="flex items-center justify-between px-5 py-3.5 border-b">
            <div className="flex items-center gap-2">
              <Network className="w-4 h-4 text-muted-foreground" />
              <span className="text-sm font-semibold text-foreground">Concept Detail</span>
            </div>
            <button onClick={() => setSelected(null)} className="text-muted-foreground hover:text-foreground p-0.5">
              <X className="w-4 h-4" />
            </button>
          </div>

          {detailLoading ? (
            <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">Loading...</div>
          ) : selected && col ? (
            <div className="flex flex-col gap-0">

              {/* Concept identity */}
              <div className="px-5 pt-5 pb-4 border-b">
                <div className="flex items-start gap-3 mb-3">
                  <span style={{ width: 10, height: 10, borderRadius: 3, background: col.bg, border: `2px solid ${col.border}`, flexShrink: 0, marginTop: 4 }} />
                  <h2 className="font-bold text-[17px] text-foreground leading-tight">{selected.node.name}</h2>
                </div>
                {selected.node.type && (
                  <span className={`type-pill type-${selected.node.type}`}>
                    {selected.node.type}
                  </span>
                )}
              </div>

              {/* What is this? */}
              {selected.node.description && (
                <div className="px-5 py-4 border-b">
                  <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">What is this?</p>
                  <p className="text-sm text-foreground leading-relaxed">{selected.node.description}</p>
                </div>
              )}

              {/* Source evidence */}
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
                    <p className="text-[11px] mt-2 font-medium" style={{ color: col.text }}>
                      {selected.source_document.title || selected.source_document.filename.split('/').pop()}
                    </p>
                  )}
                </div>
              )}

              {/* Connected concepts */}
              {selected.neighbours.length > 0 ? (
                <div className="px-5 py-4">
                  <div className="flex items-center gap-1.5 mb-3">
                    <Network className="w-3.5 h-3.5 text-muted-foreground" />
                    <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">
                      Connected concepts ({selected.neighbours.length})
                    </p>
                  </div>
                  <div className="space-y-2">
                    {selected.neighbours.map(n => {
                      const edge = selected.edges.find(
                        e => (e.source_concept_id === selected.node.id && e.target_concept_id === n.id) ||
                             (e.target_concept_id === selected.node.id && e.source_concept_id === n.id)
                      );
                      const nCol = getTypeColour(n.type);
                      return (
                        <div key={n.id} className="flex items-start gap-2.5 p-2.5 border rounded-sm hover:bg-muted/30 transition-colors">
                          <span style={{ width: 7, height: 7, borderRadius: '50%', background: nCol.dot, flexShrink: 0, marginTop: 4 }} />
                          <div className="min-w-0 flex-1">
                            <p className="text-xs font-medium text-foreground leading-tight">{n.name}</p>
                            {n.type && (
                              <p className="text-[10px] mt-0.5" style={{ color: nCol.text }}>{n.type}</p>
                            )}
                          </div>
                          {edge && (
                            <span className="text-[10px] bg-muted text-muted-foreground px-1.5 py-0.5 rounded flex-shrink-0 mt-0.5 font-mono">
                              {edge.relationship_type.replace(/_/g, ' ')}
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : (
                <div className="px-5 py-4">
                  <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">Connected concepts</p>
                  <p className="text-xs text-muted-foreground">No direct relationships found for this concept.</p>
                </div>
              )}

              {/* Tip */}
              <div className="px-5 py-4 border-t">
                <div className="callout text-xs">
                  <div className="flex items-start gap-2">
                    <Info className="w-3.5 h-3.5 text-primary mt-0.5 flex-shrink-0" />
                    <span>
                      Click any connected concept in the graph to explore it, or use the Assistant tab to ask
                      evidence-backed questions about this topic.
                    </span>
                  </div>
                </div>
              </div>

            </div>
          ) : null}
        </aside>
      )}
    </div>
  );
}
