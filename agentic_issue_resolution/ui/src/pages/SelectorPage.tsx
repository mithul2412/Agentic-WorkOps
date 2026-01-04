import { useEffect, useState } from "react";

import { getSelector, listLiveDecisions } from "../api";
import type { LiveDecision, SelectorResponse } from "../types";

export default function SelectorPage() {
  const [minSamples, setMinSamples] = useState(10);
  const [data, setData] = useState<SelectorResponse | null>(null);
  const [liveDecisions, setLiveDecisions] = useState<LiveDecision[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    Promise.all([getSelector(minSamples), listLiveDecisions(25)])
      .then(([selector, decisions]) => {
        setData(selector);
        setLiveDecisions(decisions);
        setError(null);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section>
      <header className="page-header">
        <h2>Policy Selector</h2>
        <p>Best manager policy per team and category based on judged A/B outcomes.</p>
      </header>

      <div className="card form-inline">
        <label className="label">min_samples</label>
        <input
          className="input"
          type="number"
          min={1}
          value={minSamples}
          onChange={(e) => setMinSamples(Number(e.target.value))}
        />
        <button className="btn primary" onClick={load}>
          Refresh
        </button>
      </div>

      {loading && <p>Loading selector view...</p>}
      {error && <p className="error-text">{error}</p>}
      {data && (
        <>
          <div className="card">
            <p>
              Category key: <span className="mono">{data.category_key}</span>
            </p>
            <p>
              Default policy: <span className="mono">{data.default_policy_id}</span>
            </p>
            <p>
              Epsilon: <span className="mono">{data.epsilon}</span>
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Category</th>
                    <th>Policy</th>
                    <th>Wins</th>
                    <th>Losses</th>
                    <th>Ties</th>
                    <th>Total</th>
                    <th>Win Rate</th>
                    <th>Best</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row) => (
                    <tr key={`${row.category}-${row.policy_id}`} className={row.best_policy ? "best-row" : ""}>
                      <td>{row.category}</td>
                      <td>{row.policy_id}</td>
                      <td>{row.wins}</td>
                      <td>{row.losses}</td>
                      <td>{row.ties}</td>
                      <td>{row.total}</td>
                      <td>{row.win_rate.toFixed(3)}</td>
                      <td>{row.best_policy ? "YES" : "NO"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="card">
            <h3>Recent Live Decisions</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Ticket</th>
                    <th>Category (est/actual)</th>
                    <th>Policy</th>
                    <th>Explored</th>
                    <th>Runtime</th>
                    <th>Cost</th>
                    <th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {liveDecisions.map((row) => (
                    <tr key={row.decision_id}>
                      <td>{row.ticket_id}</td>
                      <td>{`${row.category_estimate || "-"} / ${row.category_actual || "-"}`}</td>
                      <td>{row.selected_policy_id || "-"}</td>
                      <td>{row.explored ? "YES" : "NO"}</td>
                      <td>{row.runtime_ms} ms</td>
                      <td>{row.cost_proxy.toFixed(4)}</td>
                      <td>{new Date(row.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
