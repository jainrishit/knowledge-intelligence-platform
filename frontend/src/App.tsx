import { BrowserRouter, Routes, Route, NavLink, useParams, Navigate, useNavigate } from 'react-router-dom';
import WorkspaceList from '@/pages/WorkspaceList';
import DocumentUpload from '@/pages/DocumentUpload';
import GraphExplorer from '@/pages/GraphExplorer';
import AssistantChat from '@/pages/AssistantChat';
import DeliverableGenerator from '@/pages/DeliverableGenerator';
import { ArrowLeft } from 'lucide-react';

const TABS = [
  { path: 'documents',    label: 'Documents',       step: 1, hint: 'Upload assets' },
  { path: 'graph',        label: 'Knowledge Graph', step: 2, hint: 'Explore concepts' },
  { path: 'chat',         label: 'Assistant',       step: 3, hint: 'Ask questions' },
  { path: 'deliverables', label: 'Deliverables',    step: 4, hint: 'Generate outputs' },
];

function WorkspaceNav() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const base = `/workspace/${workspaceId}`;
  const navigate = useNavigate();

  return (
    <div className="border-b bg-white">
      <div className="px-8 pt-3 pb-0 flex items-center gap-2">
        <button
          onClick={() => navigate('/')}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeft className="w-3 h-3" />
          All workspaces
        </button>
      </div>
      <nav className="flex px-8 gap-0 mt-1">
        {TABS.map(t => (
          <NavLink
            key={t.path}
            to={`${base}/${t.path}`}
            className={({ isActive }) =>
              `group flex items-center gap-2.5 px-4 py-3 border-b-2 transition-colors text-sm ${
                isActive
                  ? 'border-foreground text-foreground'
                  : 'border-transparent text-muted-foreground hover:text-foreground hover:border-border'
              }`
            }
          >
            {({ isActive }) => (
              <>
                <span className={`step-dot text-[10px] ${isActive ? 'step-dot-active' : 'step-dot-pending'}`}>
                  {t.step}
                </span>
                <span>
                  <span className={`block font-medium leading-tight ${isActive ? 'text-foreground' : ''}`}>
                    {t.label}
                  </span>
                  <span className="block text-[10px] text-muted-foreground leading-tight mt-0.5">{t.hint}</span>
                </span>
              </>
            )}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}

function WorkspaceLayout() {
  return (
    <div className="flex flex-col min-h-screen">
      <WorkspaceNav />
      <div className="flex-1 bg-background">
        <Routes>
          <Route path="documents"    element={<DocumentUpload />} />
          <Route path="graph"        element={<GraphExplorer />} />
          <Route path="chat"         element={<AssistantChat />} />
          <Route path="deliverables" element={<DeliverableGenerator />} />
          <Route path="*"            element={<Navigate to="documents" replace />} />
        </Routes>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-background">
        <header className="border-b bg-white px-8 flex items-center h-14 gap-3">
          <NavLink to="/" className="flex items-center gap-2.5 font-semibold text-[15px] tracking-tight text-foreground">
            <span className="w-7 h-7 bg-foreground text-background flex items-center justify-center text-xs font-bold rounded-sm">
              KI
            </span>
            Knowledge Intelligence Platform
          </NavLink>
          <span className="ml-2 text-xs text-muted-foreground border px-2 py-0.5 rounded-full">IBM Consulting</span>
        </header>

        <Routes>
          <Route path="/"                         element={<WorkspaceList />} />
          <Route path="/workspace/:workspaceId/*" element={<WorkspaceLayout />} />
        </Routes>
      </div>
    </BrowserRouter>
  );
}
