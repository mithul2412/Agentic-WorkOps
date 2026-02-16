import { useEffect, useState } from "react";

import { getOperateRun, judgeAB, listABRuns, runAB } from "../api";
import type { ABRunRecord, OperateRunDetail, OperateRunResponse } from "../types";

export default function OperatePage() {
  const [source, setSource] = useState("tasks_json");
  const [tasksPath, setTasksPath] = useState("/Users/myth/Documents/VSCode/Codetor/agentic_issue_resolution/samples/tasks.json");
  const [ticketIdsText, setTicketIdsText] = useState("ENG-123");
  const [policyA, setPolicyA] = useState("manager_ollama_qwen25_sft_v1");
  const [policyB, setPolicyB] = useState("manager_ollama_gemma2_local_v1");
  const [judgePolicy, setJudgePolicy] = useState("judge_groq_v1");
  const [maxTasks, setMaxTasks] = useState("10");
  const [seed, setSeed] = useState("42");
  const [runResponse, setRunResponse] = useState<OperateRunResponse | null>(null);
  const [runDetail, setRunDetail] = useState<OperateRunDetail | null>(null);
  const [judgeResponse, setJudgeResponse] = useState<Record<string, unknown> | null>(null);
  const [recentRuns, setRecentRuns] = useState<ABRunRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadRecentRuns = async () => {
    try {
      const rows = await listABRuns(10);
      setRecentRuns(rows);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  useEffect(() => {
    void loadRecentRuns();
  }, []);

  const startRun = async () => {
    setBusy(true);
    setError(null);
    try {
      const ticketIds = ticketIdsText
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      const response = await runAB({
        source,
        tasks_path: source === "tasks_json" ? tasksPath : null,
        ticket_ids: source === "saved_jira" ? ticketIds : [],
        policy_a_id: policyA,
        policy_b_id: policyB,
        max_tasks: maxTasks.trim() ? Number(maxTasks) : null,
        seed: seed.trim() ? Number(seed) : 42
      });
      setRunResponse(response);
      const detail = await getOperateRun(response.ab_run_id);
      setRunDetail(detail);
      await loadRecentRuns();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runJudge = async () => {
    if (!runResponse) return;
    setBusy(true);
    setError(null);
    try {
      const response = await judgeAB({
        ab_run_id: runResponse.ab_run_id,
        judge_policy_id: judgePolicy,
        category_key: "team_profile|ticket_type|risk_tier"
      });
      setJudgeResponse(response as Record<string, unknown>);
      const detail = await getOperateRun(runResponse.ab_run_id);
      setRunDetail(detail);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <header className="page-header">
        <h2>Operate A/B Lab</h2>
        <p>Run policy experiments, judge outcomes, and persist RLAIF preference signals.</p>
      </header>

      <div className="card form-stack">
        <label className="label">Source</label>
        <select className="input" value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="tasks_json">tasks_json</option>
          <option value="saved_jira">saved_jira</option>
        </select>
        {source === "tasks_json" ? (
          <>
            <label className="label">tasks_path</label>
            <input className="input" value={tasksPath} onChange={(e) => setTasksPath(e.target.value)} />
          </>
        ) : (
          <>
            <label className="label">ticket_ids (comma separated)</label>
            <textarea
              className="input"
              value={ticketIdsText}
              onChange={(e) => setTicketIdsText(e.target.value)}
              placeholder="ENG-123, ENG-124"
            />
          </>
        )}
        <label className="label">Policy A</label>
        <input className="input" value={policyA} onChange={(e) => setPolicyA(e.target.value)} />
        <label className="label">Policy B</label>
        <input className="input" value={policyB} onChange={(e) => setPolicyB(e.target.value)} />
        <label className="label">max_tasks</label>
        <input className="input" value={maxTasks} onChange={(e) => setMaxTasks(e.target.value)} />
        <label className="label">seed</label>
        <input className="input" value={seed} onChange={(e) => setSeed(e.target.value)} />
        <button className="btn primary" disabled={busy} onClick={startRun}>
          Start A/B Run
        </button>
      </div>

      {runResponse && (
        <div className="card">
          <h3>Run Result</h3>
          <pre>{JSON.stringify(runResponse, null, 2)}</pre>
          <label className="label">Judge Policy</label>
          <input className="input" value={judgePolicy} onChange={(e) => setJudgePolicy(e.target.value)} />
          <button className="btn accent" disabled={busy} onClick={runJudge}>
            Judge A vs B
          </button>
        </div>
      )}

      {judgeResponse && (
        <div className="card">
          <h3>Judge Outcome</h3>
          <pre>{JSON.stringify(judgeResponse, null, 2)}</pre>
        </div>
      )}

      {runDetail && (
        <div className="card">
          <h3>Run Detail</h3>
          <p>Items: {runDetail.items.length}</p>
          <p>Judgments: {runDetail.judgments.length}</p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Ticket</th>
                  <th>Category</th>
                  <th>A Runtime</th>
                  <th>B Runtime</th>
                  <th>A Cost</th>
                  <th>B Cost</th>
                </tr>
              </thead>
              <tbody>
                {runDetail.items.map((item) => {
                  const row = item as Record<string, unknown>;
                  const ma = (row.metrics_a || {}) as Record<string, unknown>;
                  const mb = (row.metrics_b || {}) as Record<string, unknown>;
                  return (
                    <tr key={String(row.item_id)}>
                      <td>{String(row.task_id)}</td>
                      <td>{String(row.ticket_id || "-")}</td>
                      <td>{String(row.category_actual || row.category_estimate || "-")}</td>
                      <td>{String(ma.runtime_ms || "-")}</td>
                      <td>{String(mb.runtime_ms || "-")}</td>
                      <td>{String(ma.cost_proxy || "-")}</td>
                      <td>{String(mb.cost_proxy || "-")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card">
        <h3>Recent A/B Runs</h3>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Run ID</th>
                <th>Source</th>
                <th>Policies</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {recentRuns.map((row) => (
                <tr key={row.ab_run_id}>
                  <td className="mono">{row.ab_run_id}</td>
                  <td>{row.source}</td>
                  <td>{`${row.policy_a_id} vs ${row.policy_b_id}`}</td>
                  <td>{row.status}</td>
                  <td>{`${row.completed_tasks}/${row.total_tasks}`}</td>
                  <td>{new Date(row.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {error && <p className="error-text">{error}</p>}
    </section>
  );
}
