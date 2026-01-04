import { FormEvent, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { addStoryEvent, approveTicket, getFlow, getTicketStory, openFlowStream } from "../api";
import FlowLine from "../components/FlowLine";
import type { FlowSnapshot, TicketStory } from "../types";

export default function TicketStoryPage() {
  const { ticketId } = useParams();
  const [story, setStory] = useState<TicketStory | null>(null);
  const [flow, setFlow] = useState<FlowSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [streamStatus, setStreamStatus] = useState<"connecting" | "live" | "disconnected">("connecting");
  const [streamError, setStreamError] = useState<string | null>(null);
  const [eventKind, setEventKind] = useState("MEETING_COMPLETED");
  const [actor, setActor] = useState("manager");
  const [team, setTeam] = useState("platform");
  const [eventText, setEventText] = useState("");
  const [reviewer, setReviewer] = useState("senior.reviewer");
  const [approveNote, setApproveNote] = useState("Approved for next stage");
  const storySizeRef = useRef(0);

  const load = (withSpinner = true) => {
    if (!ticketId) return;
    if (withSpinner) {
      setLoading(true);
    }
    Promise.all([getTicketStory(ticketId), getFlow(ticketId)])
      .then(([storyData, flowData]) => {
        setStory(storyData);
        storySizeRef.current = storyData.timeline.length;
        setFlow(flowData);
        setError(null);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => {
        if (withSpinner) {
          setLoading(false);
        }
      });
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticketId]);

  useEffect(() => {
    if (!ticketId) return;
    setStreamStatus("connecting");
    const source = openFlowStream(
      ticketId,
      (nextFlow) => {
        setFlow(nextFlow);
        setStreamStatus("live");
        setStreamError(null);
        if (nextFlow.timeline_size !== storySizeRef.current) {
          getTicketStory(ticketId)
            .then((updated) => {
              setStory(updated);
              storySizeRef.current = updated.timeline.length;
            })
            .catch((err: Error) => setError(err.message));
        }
      },
      (message) => {
        setStreamStatus("disconnected");
        setStreamError(message);
      }
    );
    return () => {
      source.close();
    };
  }, [ticketId]);

  const onAddEvent = async (event: FormEvent) => {
    event.preventDefault();
    if (!ticketId) return;
    try {
      await addStoryEvent(ticketId, {
        kind: eventKind,
        source: "MANUAL",
        actor,
        team,
        payload: { note: eventText }
      });
      setEventText("");
      load(false);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const onApprove = async () => {
    if (!ticketId) return;
    try {
      await approveTicket(ticketId, reviewer, approveNote);
      load(false);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  if (!ticketId) return <p className="error-text">Ticket ID is missing.</p>;
  if (loading) return <p>Loading story...</p>;
  if (!story) return <p className="error-text">{error || "Ticket not found"}</p>;

  return (
    <section>
      <header className="page-header">
        <h2>{story.ticket_id}</h2>
        <p>{story.summary}</p>
        <p className="meta-line">
          Live stream:{" "}
          <span className={`stream-badge stream-${streamStatus}`}>{streamStatus.toUpperCase()}</span>
        </p>
        {streamError ? <p className="error-text">{streamError}</p> : null}
      </header>

      <FlowLine flow={flow} />

      <div className="story-layout">
        <div className="card">
          <h3>Story Timeline</h3>
          <p className="meta-line">Status: {story.status}</p>
          <p className="meta-line">Risk: {story.risk_tier || "n/a"}</p>
          <p className="meta-line">Assignee: {story.assignee || "unassigned"}</p>
          <div className="timeline">
            {story.timeline.map((event) => (
              <article key={event.event_id} className="timeline-item">
                <div className="timeline-head">
                  <span className="pill">{event.source}</span>
                  <span className="mono">{event.kind}</span>
                </div>
                <p className="meta-line">
                  {new Date(event.ts).toLocaleString()} | {event.actor || "system"} | {event.team || "general"}
                </p>
                <pre>{JSON.stringify(event.payload, null, 2)}</pre>
              </article>
            ))}
          </div>
        </div>

        <aside className="stack">
          <div className="card">
            <h3>Add Manual Event</h3>
            <form onSubmit={onAddEvent} className="form-stack">
              <label className="label">Kind</label>
              <select className="input" value={eventKind} onChange={(e) => setEventKind(e.target.value)}>
                <option>ASSIGNMENT_COMMITTED</option>
                <option>MEETING_SCHEDULED</option>
                <option>MEETING_COMPLETED</option>
                <option>MEETING_NOTES_ADDED</option>
                <option>TEAM_INVOLVED</option>
                <option>QA_REQUESTED</option>
                <option>PR_CREATED</option>
                <option>PR_APPROVED</option>
                <option>PR_MERGED</option>
                <option>CONFLUENCE_UPDATED</option>
              </select>
              <label className="label">Actor</label>
              <input className="input" value={actor} onChange={(e) => setActor(e.target.value)} />
              <label className="label">Team</label>
              <input className="input" value={team} onChange={(e) => setTeam(e.target.value)} />
              <label className="label">Notes</label>
              <textarea className="input" value={eventText} onChange={(e) => setEventText(e.target.value)} />
              <button className="btn primary" type="submit">
                Add Event
              </button>
            </form>
          </div>

          <div className="card">
            <h3>Approval Control</h3>
            <label className="label">Reviewer</label>
            <input className="input" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
            <label className="label">Comment</label>
            <textarea className="input" value={approveNote} onChange={(e) => setApproveNote(e.target.value)} />
            <button className="btn accent" onClick={onApprove}>
              Approve Ticket Flow
            </button>
          </div>

          <div className="card">
            <h3>Artifacts</h3>
            <pre>{JSON.stringify(story.artifacts, null, 2)}</pre>
          </div>
        </aside>
      </div>
      {error && <p className="error-text">{error}</p>}
    </section>
  );
}
