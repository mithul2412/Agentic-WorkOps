import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { getFlow, getMetricsSummary, getModelOpsSummary, getTicketStory, listTickets } from "../api";
import FlowLine from "../components/FlowLine";
import type { FlowSnapshot, MetricsSummary, ModelOpsSummary, StoryEvent, TicketListItem, TicketStory } from "../types";

function asPercent(value: number | null): string {
  if (value === null) return "n/a";
  return `${(value * 100).toFixed(1)}%`;
}

function previewPayload(payload: Record<string, unknown>): string {
  const text = JSON.stringify(payload);
  if (!text) return "{}";
  return text.length > 180 ? `${text.slice(0, 177)}...` : text;
}

export default function DashboardPage() {
  const [tickets, setTickets] = useState<TicketListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null);
  const [modelOps, setModelOps] = useState<ModelOpsSummary | null>(null);

  const [selectedTicketId, setSelectedTicketId] = useState<string | null>(null);
  const [selectedStory, setSelectedStory] = useState<TicketStory | null>(null);
  const [selectedFlow, setSelectedFlow] = useState<FlowSnapshot | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const loadBoard = async () => {
    setLoading(true);
    try {
      const [rows, metricsSummary, modelOpsSummary] = await Promise.all([
        listTickets(),
        getMetricsSummary(),
        getModelOpsSummary()
      ]);
      setTickets(rows);
      setMetrics(metricsSummary);
      setModelOps(modelOpsSummary);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const loadTicketDetail = async (ticketId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    try {
      const [storyData, flowData] = await Promise.all([getTicketStory(ticketId), getFlow(ticketId)]);
      setSelectedStory(storyData);
      setSelectedFlow(flowData);
    } catch (err) {
      setDetailError((err as Error).message);
    } finally {
      setDetailLoading(false);
    }
  };

  useEffect(() => {
    void loadBoard();
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tickets;
    return tickets.filter(
      (ticket) =>
        ticket.ticket_id.toLowerCase().includes(q) ||
        ticket.summary.toLowerCase().includes(q) ||
        String(ticket.status).toLowerCase().includes(q) ||
        String(ticket.current_step).toLowerCase().includes(q) ||
        (ticket.assignee || "").toLowerCase().includes(q)
    );
  }, [tickets, query]);

  useEffect(() => {
    if (!filtered.length) {
      setSelectedTicketId(null);
      setSelectedStory(null);
      setSelectedFlow(null);
      return;
    }
    const hasSelection = selectedTicketId ? filtered.some((row) => row.ticket_id === selectedTicketId) : false;
    if (!hasSelection) {
      setSelectedTicketId(filtered[0].ticket_id);
    }
  }, [filtered, selectedTicketId]);

  useEffect(() => {
    if (!selectedTicketId) return;
    void loadTicketDetail(selectedTicketId);
  }, [selectedTicketId]);

  const selectedTicket = useMemo(
    () => (selectedTicketId ? tickets.find((row) => row.ticket_id === selectedTicketId) || null : null),
    [tickets, selectedTicketId]
  );

  const recentEvents = useMemo<StoryEvent[]>(() => {
    if (!selectedStory) return [];
    return [...selectedStory.timeline].slice(-12).reverse();
  }, [selectedStory]);

  const refreshAll = () => {
    void loadBoard();
    if (selectedTicketId) {
      void loadTicketDetail(selectedTicketId);
    }
  };

  const totalTickets = metrics?.totals.tickets ?? tickets.length;
  const completedTickets = metrics?.totals.completed_tickets ?? tickets.filter((row) => row.status === "COMPLETED").length;
  const avgCycle = metrics?.triage_to_fix_cycle_time.avg_minutes ?? null;
  const reopenRate = metrics?.reopen_regression_rate.rate ?? null;
  const fallbackRate = modelOps?.manager.fallback_rate ?? null;
  const explorationRate = modelOps?.selector.exploration_rate ?? null;

  return (
    <section className="command-center">
      <header className="page-header command-header">
        <div>
          <h2>Ticket Command Center</h2>
          <p>Track stage progression, recent events, and current state for every ticket in one screen.</p>
        </div>
        <button className="btn subtle" onClick={refreshAll} disabled={loading || detailLoading}>
          {loading || detailLoading ? "Refreshing..." : "Refresh"}
        </button>
      </header>

      <div className="card compact-card board-top-strip">
        <div className="board-search-wrap">
          <label className="label" htmlFor="ticket-search">
            Search tickets
          </label>
          <input
            id="ticket-search"
            className="input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="ID, summary, status, stage, assignee"
          />
          <p className="meta-line">Showing {filtered.length} of {tickets.length} tickets</p>
        </div>

        <div className="kpi-strip">
          <div className="kpi-chip">
            <span>Tickets</span>
            <strong>{totalTickets}</strong>
          </div>
          <div className="kpi-chip">
            <span>Completed</span>
            <strong>{completedTickets}</strong>
          </div>
          <div className="kpi-chip">
            <span>Avg Cycle</span>
            <strong>{avgCycle === null ? "n/a" : `${avgCycle.toFixed(1)}m`}</strong>
          </div>
          <div className="kpi-chip">
            <span>Reopen Rate</span>
            <strong>{asPercent(reopenRate)}</strong>
          </div>
          <div className="kpi-chip">
            <span>Fallback</span>
            <strong>{asPercent(fallbackRate)}</strong>
          </div>
          <div className="kpi-chip">
            <span>Exploration</span>
            <strong>{asPercent(explorationRate)}</strong>
          </div>
        </div>
      </div>

      <div className="board-grid">
        <section className="card board-pane">
          <div className="board-pane-header">
            <h3>Ticket Board</h3>
            <span className="meta-line">{loading ? "Loading..." : `${filtered.length} rows`}</span>
          </div>

          <div className="board-ticket-table-wrap">
            <table className="board-ticket-table">
              <thead>
                <tr>
                  <th>Ticket</th>
                  <th>Status</th>
                  <th>Stage</th>
                  <th>Risk</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((ticket) => (
                  <tr
                    key={ticket.ticket_id}
                    className={selectedTicketId === ticket.ticket_id ? "board-row-selected" : ""}
                    onClick={() => setSelectedTicketId(ticket.ticket_id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setSelectedTicketId(ticket.ticket_id);
                      }
                    }}
                    tabIndex={0}
                    role="button"
                    aria-label={`Open ${ticket.ticket_id}`}
                  >
                    <td>
                      <div className="board-ticket-id mono">{ticket.ticket_id}</div>
                      <div className="board-ticket-summary">{ticket.summary}</div>
                    </td>
                    <td>
                      <span className="pill board-ticket-status">{ticket.status}</span>
                    </td>
                    <td className="mono">{ticket.current_step}</td>
                    <td>{ticket.risk_tier || "n/a"}</td>
                    <td>{new Date(ticket.updated_at).toLocaleString()}</td>
                  </tr>
                ))}

                {!loading && filtered.length === 0 ? (
                  <tr>
                    <td className="empty-row" colSpan={5}>
                      No tickets matched your filter.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </section>

        <section className="card board-pane ticket-detail-pane">
          <div className="board-pane-header">
            <div>
              <h3>{selectedTicket ? selectedTicket.ticket_id : "Ticket Details"}</h3>
              <p className="meta-line">{selectedTicket?.summary || "Select a ticket from the board."}</p>
            </div>
            {selectedTicket ? (
              <Link className="btn subtle" to={`/tickets/${selectedTicket.ticket_id}`}>
                Open Full Ticket
              </Link>
            ) : null}
          </div>

          {selectedTicket ? (
            <>
              <div className="detail-meta-grid">
                <div className="detail-meta-item">
                  <span>Status</span>
                  <strong>{selectedTicket.status}</strong>
                </div>
                <div className="detail-meta-item">
                  <span>Stage</span>
                  <strong>{selectedTicket.current_step}</strong>
                </div>
                <div className="detail-meta-item">
                  <span>Risk</span>
                  <strong>{selectedTicket.risk_tier || "n/a"}</strong>
                </div>
                <div className="detail-meta-item">
                  <span>Assignee</span>
                  <strong>{selectedTicket.assignee || "unassigned"}</strong>
                </div>
              </div>

              <div className="detail-flow-wrap">
                <FlowLine flow={selectedFlow} compact />
              </div>

              <div className="detail-events-head">
                <h4>Recent Events</h4>
                <span className="meta-line">
                  {detailLoading ? "Refreshing..." : `Last update: ${new Date(selectedTicket.updated_at).toLocaleString()}`}
                </span>
              </div>

              {detailError ? <p className="error-text">{detailError}</p> : null}

              <div className="recent-events">
                {recentEvents.length ? (
                  recentEvents.map((event) => (
                    <article key={event.event_id} className="recent-event">
                      <div className="recent-event-head">
                        <span className="pill">{event.source}</span>
                        <span className="event-kind mono">{event.kind}</span>
                      </div>
                      <p className="meta-line">
                        {new Date(event.ts).toLocaleString()} · {event.actor || "system"} · {event.team || "general"}
                      </p>
                      <p className="event-preview mono">{previewPayload(event.payload)}</p>
                    </article>
                  ))
                ) : (
                  <p className="meta-line">No timeline events yet.</p>
                )}
              </div>
            </>
          ) : (
            <p className="meta-line">No ticket selected.</p>
          )}
        </section>
      </div>

      {error ? <p className="error-text">{error}</p> : null}
    </section>
  );
}
