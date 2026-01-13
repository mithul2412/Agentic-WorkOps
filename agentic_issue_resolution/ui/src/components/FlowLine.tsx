import type { FlowSnapshot } from "../types";

type Props = {
  flow: FlowSnapshot | null;
};

export default function FlowLine({ flow }: Props) {
  if (!flow) {
    return (
      <div className="card">
        <h3>LangGraph Flow</h3>
        <p className="meta-line">Flow snapshot is not available yet.</p>
      </div>
    );
  }

  return (
    <div className="card">
      <h3>LangGraph Flow</h3>
      <p className="meta-line">
        Current stage: <span className="mono">{flow.current_step}</span> | Status:{" "}
        <span className="mono">{flow.status}</span>
      </p>
      <div className="flow-line">
        {flow.nodes.map((node, index) => (
          <div className="flow-node-wrap" key={node.id}>
            <div className={`flow-node flow-${node.state}`}>
              <span className="flow-node-dot" />
              <div className="flow-node-body">
                <strong>{node.label}</strong>
                <p>{node.description}</p>
              </div>
            </div>
            {index < flow.nodes.length - 1 ? <div className="flow-connector" /> : null}
          </div>
        ))}
      </div>
      {flow.last_event ? (
        <div className="flow-last-event">
          <span className="label">Last event</span>
          <pre>{JSON.stringify(flow.last_event, null, 2)}</pre>
        </div>
      ) : null}
    </div>
  );
}
