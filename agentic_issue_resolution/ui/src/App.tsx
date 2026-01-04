import { Link, Route, Routes, useLocation } from "react-router-dom";
import DashboardPage from "./pages/DashboardPage";
import TicketStoryPage from "./pages/TicketStoryPage";
import OperatePage from "./pages/OperatePage";
import SelectorPage from "./pages/SelectorPage";

const NAV_ITEMS = [
  { to: "/", label: "Ticket Board" },
  { to: "/operate", label: "Operate A/B" },
  { to: "/selector", label: "Policy Selector" }
];

export default function App() {
  const location = useLocation();
  return (
    <div className="app-shell">
      <aside className="side-nav">
        <div className="brand">
          <h1>StoryOps</h1>
          <p>Ticket narrative and policy control center</p>
        </div>
        <nav>
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.to}
              className={`nav-link ${location.pathname === item.to ? "active" : ""}`}
              to={item.to}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </aside>
      <main className="main-view">
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
