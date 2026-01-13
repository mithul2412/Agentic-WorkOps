import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { getMetricsSummary, getModelOpsSummary, listTickets } from "../api";
import type { MetricsSummary, ModelOpsSummary, TicketListItem } from "../types";

export default function DashboardPage() {
  const [tickets, setTickets] = useState<TicketListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null);
  const [modelOps, setModelOps] = useState<ModelOpsSummary | null>(null);

  useEffect(() => {
    setLoading(true);
    Promise.all([listTickets(), getMetricsSummary(), getModelOpsSummary()])
      .then(([rows, metricsSummary, modelOpsSummary]) => {
        setTickets(rows);
        setMetrics(metricsSummary);
        setModelOps(modelOpsSummary);
        setError(null);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tickets;
    return tickets.filter(
      (ticket) =>
        ticket.ticket_id.toLowerCase().includes(q) ||
        ticket.summary.toLowerCase().includes(q) ||
        String(ticket.status).toLowerCase().includes(q) ||
        (ticket.assignee || "").toLowerCase().includes(q)
    );
  }, [tickets, query]);

  return (
    <section>
      <header className="page-header">
        <h2>Office Ticket Board</h2>
        <p>Track every ticket story from intake to merge and knowledge update.</p>
      </header>

      <div className="card">
        <label className="label">Search tickets</label>
        <input
          className="input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by ID, summary, status, assignee"
        />
      </div>

      {loading && <p>Loading tickets...</p>}
      {error && <p className="error-text">{error}</p>}
      {metrics && (
        <div className="card">
          <h3>Business Metrics Snapshot</h3>
          <p className="meta-line">
            Tickets: {metrics.totals.tickets} | Completed: {metrics.totals.completed_tickets}
          </p>
          <p className="meta-line">
            Cycle time avg/p50/p90 (min): {metrics.triage_to_fix_cycle_time.avg_minutes} /{" "}
            {metrics.triage_to_fix_cycle_time.p50_minutes} / {metrics.triage_to_fix_cycle_time.p90_minutes}
          </p>
          <p className="meta-line">
            Reopen/regression rate: {(metrics.reopen_regression_rate.rate * 100).toFixed(1)}%
          </p>
          <p className="meta-line">Handoff quality score: {(metrics.handoff_quality.avg_score * 100).toFixed(1)}%</p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Team</th>
                  <th>Best Policy</th>
                  <th>Win Rate</th>
                  <th>Samples</th>
                </tr>
              </thead>
              <tbody>
                {metrics.policy_win_rate_by_team.map((row) => (
                  <tr key={row.team_profile}>
                    <td>{row.team_profile}</td>
                    <td>{row.best_policy}</td>
                    <td>{(row.win_rate * 100).toFixed(1)}%</td>
                    <td>{row.sample_size}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {modelOps && (
        <div className="card">
          <h3>Model Ops Snapshot</h3>
          <p className="meta-line">
            Manager fallback rate: {(modelOps.manager.fallback_rate * 100).toFixed(1)}% ({modelOps.manager.fallback_decisions}/
            {modelOps.manager.decisions})
          </p>
          <p className="meta-line">
            Selector exploration rate: {(modelOps.selector.exploration_rate * 100).toFixed(1)}% ({modelOps.selector.explored_count}/
            {modelOps.selector.total_live_decisions})
          </p>
          <p className="meta-line">
            Judge confidence avg/p50/p90: {modelOps.judge_confidence.avg.toFixed(2)} / {modelOps.judge_confidence.p50.toFixed(2)} /{" "}
            {modelOps.judge_confidence.p90.toFixed(2)}
          </p>
          <p className="meta-line">
            Low-confidence skipped updates: {(modelOps.judge_confidence.low_confidence_skip_rate * 100).toFixed(1)}% (
            {modelOps.judge_confidence.skipped_low_confidence})
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Policy</th>
                  <th>Provider</th>
                  <th>Avg Runtime</th>
                  <th>Avg Cost</th>
                  <th>Samples</th>
                </tr>
              </thead>
              <tbody>
                {modelOps.policy_runtime_cost.map((row) => (
                  <tr key={row.policy_id}>
                    <td>{row.policy_id}</td>
                    <td>{row.provider}</td>
                    <td>{row.avg_runtime_ms.toFixed(1)} ms</td>
                    <td>{row.avg_cost_proxy.toFixed(6)}</td>
                    <td>{row.sample_size}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="ticket-grid">
        {filtered.map((ticket) => (
          <Link to={`/tickets/${ticket.ticket_id}`} key={ticket.ticket_id} className="ticket-card">
            <div className="ticket-card-top">
              <span className="pill">{ticket.status}</span>
              <span className="mono">{ticket.ticket_id}</span>
            </div>
            <h3>{ticket.summary}</h3>
            <p className="meta-line">Step: {ticket.current_step}</p>
            <p className="meta-line">Risk: {ticket.risk_tier || "n/a"}</p>
            <p className="meta-line">Assignee: {ticket.assignee || "unassigned"}</p>
            <p className="meta-line">Updated: {new Date(ticket.updated_at).toLocaleString()}</p>
          </Link>
        ))}
      </div>
    </section>
  );
}
