import { useEffect, useRef, useState } from 'react';
import { useParams, useLocation, useNavigate } from 'react-router-dom';
import { api } from '@/api/client';
import type { AskResponse, ChatMessage, Workspace } from '@/types/api';
import { Send, ChevronDown, ChevronUp, Loader2, MessageSquare, FileText, AlertCircle, Network, X, GitBranch, TrendingUp } from 'lucide-react';

function parseMarkdown(text: string): React.ReactNode[] {
  const lines = text.split('\n');
  const out: React.ReactNode[] = [];
  let listBuf: React.ReactNode[] = [];
  let k = 0;

  const flush = () => {
    if (listBuf.length) {
      out.push(<ul key={k++} style={{ paddingLeft: 18, margin: '4px 0 8px' }}>{listBuf}</ul>);
      listBuf = [];
    }
  };

  const inline = (raw: string): React.ReactNode[] =>
    raw.split(/(\*\*[\s\S]*?\*\*|\*[\s\S]*?\*|`[^`]+`)/g).map((seg, i) => {
      if (seg.startsWith('**') && seg.endsWith('**') && seg.length > 4)
        return <strong key={i}>{seg.slice(2, -2)}</strong>;
      if (seg.startsWith('*') && seg.endsWith('*') && seg.length > 2)
        return <em key={i}>{seg.slice(1, -1)}</em>;
      if (seg.startsWith('`') && seg.endsWith('`') && seg.length > 2)
        return <code key={i} style={{ background: '#f3f4f6', padding: '1px 5px', borderRadius: 3, fontSize: '0.88em', fontFamily: 'monospace' }}>{seg.slice(1, -1)}</code>;
      return seg;
    });

  for (const line of lines) {
    const h3 = line.match(/^###\s+(.*)/); if (h3) { flush(); out.push(<p key={k++} style={{ fontWeight: 600, margin: '8px 0 2px' }}>{inline(h3[1])}</p>); continue; }
    const h2 = line.match(/^##\s+(.*)/);  if (h2) { flush(); out.push(<p key={k++} style={{ fontWeight: 600, fontSize: '1.04em', margin: '10px 0 2px' }}>{inline(h2[1])}</p>); continue; }
    const h1 = line.match(/^#\s+(.*)/);   if (h1) { flush(); out.push(<p key={k++} style={{ fontWeight: 700, fontSize: '1.1em',  margin: '12px 0 4px' }}>{inline(h1[1])}</p>); continue; }
    const bullet   = line.match(/^[-*]\s+(.*)/);
    const numbered = line.match(/^\d+\.\s+(.*)/);
    if (bullet || numbered) { listBuf.push(<li key={k++} style={{ marginBottom: 2 }}>{inline((bullet ?? numbered)![1])}</li>); continue; }
    flush();
    if (line.trim() === '') { out.push(<span key={k++} style={{ display: 'block', height: 6 }} />); continue; }
    out.push(<span key={k++} style={{ display: 'block' }}>{inline(line)}</span>);
  }
  flush();
  return out;
}

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sources?: AskResponse['sources'];
  isError?: boolean;
}

const STARTER_QUESTIONS = [
  'What are the key concepts covered in this workspace?',
  'What are the main patterns or approaches identified?',
  'How do the concepts in this workspace connect to each other?',
  'What are the main risks or challenges identified?',
];

function SourceCitation({ sources }: { sources: AskResponse['sources'] }) {
  const [open, setOpen] = useState(false);
  if (!sources || sources.length === 0) return null;
  return (
    <div className="mt-3 pt-3 border-t border-border/50">
      <button
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-1.5 text-xs text-primary hover:underline font-medium"
      >
        <FileText className="w-3 h-3" />
        {sources.length} source{sources.length > 1 ? 's' : ''}
        {open ? <ChevronUp className="w-3 h-3 ml-0.5" /> : <ChevronDown className="w-3 h-3 ml-0.5" />}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {sources.map((s, i) => (
            <div key={i} className="border rounded p-2.5 bg-muted/30 text-xs">
              <div className="flex items-center gap-1.5 mb-1">
                <FileText className="w-3 h-3 text-muted-foreground" />
                <p className="font-semibold text-foreground">{s.document_name}</p>
              </div>
              {s.excerpt && (
                <p className="text-muted-foreground italic leading-relaxed border-l-2 border-primary/30 pl-2">
                  "{s.excerpt}"
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function isNotFound(content: string) {
  return ['could not find', 'cannot find', 'outside the documents', 'not found in the uploaded'].some(
    phrase => content.toLowerCase().includes(phrase)
  );
}

function ChatBubble({ msg }: { msg: Message }) {
  const isUser = msg.role === 'user';
  const notFound = !isUser && isNotFound(msg.content);

  if (isUser) {
    return (
      <div className="flex justify-end mb-5">
        <div className="max-w-[75%] px-4 py-2.5 bg-foreground text-background text-sm leading-relaxed rounded-sm">
          <p className="whitespace-pre-wrap">{msg.content}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start mb-5 gap-2.5">
      <div className="w-7 h-7 rounded-sm bg-muted border flex items-center justify-center flex-shrink-0 mt-0.5">
        <MessageSquare className="w-3.5 h-3.5 text-muted-foreground" />
      </div>
      <div className={`max-w-[80%] px-4 py-3 text-sm leading-relaxed border rounded-sm ${
        notFound ? 'bg-amber-50 border-amber-200' : 'bg-white'
      }`}>
        {notFound && (
          <div className="flex items-center gap-1.5 mb-2 text-xs text-amber-700 font-medium">
            <AlertCircle className="w-3.5 h-3.5" />
            Not found in this workspace's documents
          </div>
        )}
        <div className="text-foreground">{parseMarkdown(msg.content)}</div>
        {!notFound && msg.sources && <SourceCitation sources={msg.sources} />}
      </div>
    </div>
  );
}

function WorkspaceKnowledgeBadge({ ws }: { ws: Workspace | null }) {
  if (!ws || ws.concept_count === 0) return null;
  return (
    <div className="mt-3 flex items-center gap-3 flex-wrap">
      <span className="inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 bg-muted/60 border rounded text-muted-foreground">
        <Network className="w-3 h-3 text-primary" />
        <span className="font-semibold text-foreground">{ws.concept_count.toLocaleString()}</span> concepts
      </span>
      {ws.relationship_count > 0 && (
        <span className="inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 bg-muted/60 border rounded text-muted-foreground">
          <GitBranch className="w-3 h-3 text-primary" />
          <span className="font-semibold text-foreground">{ws.relationship_count}</span> relationships
        </span>
      )}
      {ws.pattern_count > 0 && (
        <span className="inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 bg-muted/60 border rounded text-muted-foreground">
          <TrendingUp className="w-3 h-3 text-primary" />
          <span className="font-semibold text-foreground">{ws.pattern_count}</span> patterns
        </span>
      )}
    </div>
  );
}

export default function AssistantChat() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const wsId = Number(workspaceId);
  const location = useLocation();
  const navigate = useNavigate();
  const prefillState = (location.state as { prefill?: string; conceptName?: string } | null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState(prefillState?.prefill ?? '');
  const [graphContext, setGraphContext] = useState<string | null>(prefillState?.conceptName ?? null);
  const [loading, setLoading] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.workspaces.get(wsId).then(setWorkspace).catch(() => null);
    api.assistant.chatHistory(wsId).then((hist: ChatMessage[]) => {
      setMessages(
        hist.map(m => ({
          id: String(m.id),
          role: m.role,
          content: m.content,
          sources: [],
        }))
      );
      setHistoryLoaded(true);
    });
  }, [wsId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const send = async (question?: string) => {
    const q = (question ?? input).trim();
    if (!q || loading) return;
    setInput('');
    const userMsg: Message = { id: `u-${Date.now()}`, role: 'user', content: q };
    setMessages(prev => [...prev, userMsg]);
    setLoading(true);
    try {
      const resp = await api.assistant.ask(wsId, q);
      setMessages(prev => [...prev, {
        id: `a-${Date.now()}`,
        role: 'assistant',
        content: resp.answer,
        sources: resp.sources,
      }]);
    } catch (e: unknown) {
      setMessages(prev => [...prev, {
        id: `err-${Date.now()}`,
        role: 'assistant',
        content: `Error: ${e instanceof Error ? e.message : 'Unknown error'}`,
        sources: [],
        isError: true,
      }]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const empty = messages.length === 0 && historyLoaded;

  return (
    <div className="flex flex-col h-[calc(100vh-140px)] max-w-3xl mx-auto px-8">

      <div className="py-5 border-b flex-shrink-0">
        <h1 className="text-2xl font-semibold text-foreground">Knowledge Assistant</h1>
        <p className="text-sm text-muted-foreground mt-1 leading-relaxed">
          Ask anything about this workspace's documents. Every answer is grounded exclusively in your uploaded
          content and cites its exact source.
        </p>

        <WorkspaceKnowledgeBadge ws={workspace} />

        {graphContext && (
          <div className="mt-3 inline-flex items-center gap-2 px-3 py-1.5 bg-primary/5 border border-primary/20 rounded text-xs">
            <Network className="w-3.5 h-3.5 text-primary flex-shrink-0" />
            <span className="text-foreground">
              Graph context: <span className="font-semibold">{graphContext}</span>
            </span>
            <button
              onClick={() => { setGraphContext(null); navigate('.', { replace: true, state: {} }); }}
              className="text-muted-foreground hover:text-foreground ml-1"
              title="Clear graph context"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto py-6">

        {empty && (
          <div className="mb-6">
            <div className="callout mb-5">
              <p className="font-semibold text-foreground text-sm mb-1">How the assistant works</p>
              <ul className="text-xs text-muted-foreground space-y-1 mt-2">
                <li className="flex items-start gap-2">
                  <span className="w-1 h-1 rounded-full bg-primary mt-1.5 flex-shrink-0" />
                  Searches the knowledge graph compiled from your documents — not the internet.
                </li>
                <li className="flex items-start gap-2">
                  <span className="w-1 h-1 rounded-full bg-primary mt-1.5 flex-shrink-0" />
                  Every answer cites its exact source document and verbatim excerpt.
                </li>
                <li className="flex items-start gap-2">
                  <span className="w-1 h-1 rounded-full bg-primary mt-1.5 flex-shrink-0" />
                  If the answer isn't in your documents, it says so. It never guesses.
                </li>
              </ul>
            </div>

            {workspace && workspace.concept_count === 0 && (
              <div className="callout callout-amber mb-5 text-xs">
                <span className="font-semibold text-foreground">No knowledge compiled yet.</span>{' '}
                Upload documents in the Documents tab first — the assistant answers from your compiled knowledge graph.
              </div>
            )}

            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">Try asking</p>
            <div className="flex flex-col gap-2">
              {STARTER_QUESTIONS.map(q => (
                <button
                  key={q}
                  onClick={() => send(q)}
                  className="text-left text-sm text-foreground px-4 py-2.5 border bg-white hover:bg-muted/40 transition-colors rounded-sm flex items-center justify-between group"
                >
                  <span>{q}</span>
                  <Send className="w-3.5 h-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0 ml-3" />
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map(msg => <ChatBubble key={msg.id} msg={msg} />)}

        {loading && (
          <div className="flex justify-start mb-5 gap-2.5">
            <div className="w-7 h-7 rounded-sm bg-muted border flex items-center justify-center flex-shrink-0">
              <MessageSquare className="w-3.5 h-3.5 text-muted-foreground" />
            </div>
            <div className="px-4 py-3 bg-white border rounded-sm">
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Searching knowledge graph...
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="py-4 border-t flex-shrink-0">
        <form
          onSubmit={e => { e.preventDefault(); send(); }}
          className="flex gap-2"
        >
          <input
            ref={inputRef}
            className="flex-1 border px-4 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-foreground bg-white"
            placeholder="Ask about this workspace's documents..."
            value={input}
            onChange={e => setInput(e.target.value)}
            disabled={loading}
          />
          <button
            type="submit"
            disabled={!input.trim() || loading}
            className="px-4 py-2.5 bg-foreground text-background disabled:opacity-40 hover:opacity-80 transition-opacity"
            title="Send"
          >
            <Send className="w-4 h-4" />
          </button>
        </form>
        <p className="text-[11px] text-muted-foreground mt-2">
          Answers are sourced exclusively from documents in this workspace.
        </p>
      </div>
    </div>
  );
}
