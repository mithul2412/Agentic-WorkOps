import type { FlowSnapshot } from "../types";

type Props = {
  flow: FlowSnapshot | null;
  compact?: boolean;
};

function compactLastEvent(value: Record<string, unknown>): string {
  const step = typeof value.step === "string" ? value.step.trim() : "";
  const eventType = typeof value.event_type === "string" ? value.event_type.trim() : "";
  const payload = value.payload && typeof value.payload === "object" ? JSON.stringify(value.payload) : "";
  const prefix = [step, eventType].filter(Boolean).join(" · ");
  const text = [prefix, payload].filter(Boolean).join(" · ");
  if (!text) return "{}";
  return text.length > 140 ? `${text.slice(0, 137)}...` : text;
}

export default function FlowLine({ flow, compact = false }: Props) {
  if (!flow) {
    return (
      <div className={`card flow-card ${compact ? "flow-compact" : ""}`}>
        <h3>LangGraph Flow</h3>
        <p className="meta-line">Flow snapshot is not available yet.</p>
      </div>
    );
  }

  return (
    <div className={`card flow-card ${compact ? "flow-compact" : ""}`}>
      <h3>LangGraph Flow</h3>
      <p className="meta-line">
        Current stage: <span className="mono">{flow.current_step}</span> | Status:{" "}
        <span className="mono">{flow.status}</span>
      </p>
      <div className={`flow-line ${compact ? "flow-line-compact" : ""}`}>
        {flow.nodes.map((node, index) => (
          <div className="flow-node-wrap" key={node.id}>
            <div className={`flow-node flow-${node.state}`}>
              <span className="flow-node-dot" />
              <div className="flow-node-body">
                <strong>{node.label}</strong>
                {!compact ? <p>{node.description}</p> : null}
              </div>
            </div>
            {index < flow.nodes.length - 1 ? <div className="flow-connector" /> : null}
          </div>
        ))}
      </div>
      {flow.last_event ? (
        <div className="flow-last-event">
          <span className="label">Last event</span>
          {!compact ? (
            <pre>{JSON.stringify(flow.last_event, null, 2)}</pre>
          ) : (
            <p className="meta-line mono">{compactLastEvent(flow.last_event)}</p>
          )}
        </div>
      ) : null}
    </div>
  );
}
