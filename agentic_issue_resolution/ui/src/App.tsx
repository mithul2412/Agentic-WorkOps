import { Link, Route, Routes, useLocation } from "react-router-dom";
import DashboardPage from "./pages/DashboardPage";
import TicketStoryPage from "./pages/TicketStoryPage";
import OperatePage from "./pages/OperatePage";
import SelectorPage from "./pages/SelectorPage";

export default function App() {
  const location = useLocation();
  const isBoard = location.pathname === "/";

  return (
    <div className="app-shell">
      <header className="top-nav">
        <div className="top-brand-wrap">
          <Link to="/" className="top-brand-link">
            StoryOps
          </Link>
          <p className="top-subtitle">Ticket command center</p>
        </div>

        <nav className="top-actions" aria-label="Primary actions">
          <Link
            to="/operate"
            className={`top-action-btn ${location.pathname.startsWith("/operate") ? "active" : ""}`}
            data-tooltip="Run and compare manager policies"
          >
            Operate A/B
          </Link>
          <Link
            to="/selector"
            className={`top-action-btn ${location.pathname.startsWith("/selector") ? "active" : ""}`}
            data-tooltip="Inspect/monitor policy win-rates and category routing"
          >
            Policy Selector
          </Link>
        </nav>
      </header>

      <main className={`main-view ${isBoard ? "main-view-board" : ""}`}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/tickets/:ticketId" element={<TicketStoryPage />} />
          <Route path="/operate" element={<OperatePage />} />
          <Route path="/selector" element={<SelectorPage />} />
        </Routes>
      </main>
    </div>
  );
}
