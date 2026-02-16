import { Fragment, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { addStoryEvent, approveTicket, getFlow, getTicketStory, openFlowStream } from "../api";
import FlowLine from "../components/FlowLine";
import type { FlowSnapshot, PatchArtifactDetail, TicketArtifacts, TicketStory } from "../types";

type DiffRawLine = {
  kind: "context" | "add" | "del" | "meta";
  text: string;
  oldLine: number | null;
  newLine: number | null;
};

type DiffDisplayRow = {
  rowType: "context" | "add" | "del" | "change" | "meta";
  oldLine: number | null;
  oldText: string;
  newLine: number | null;
  newText: string;
};

type DiffHunk = {
  header: string;
  rows: DiffDisplayRow[];
};

type DiffFile = {
  oldPath: string;
  newPath: string;
  hunks: DiffHunk[];
};

type ParsedFile = {
  oldPath: string;
  newPath: string;
  hunks: Array<{ header: string; lines: DiffRawLine[] }>;
};

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

function toLocalDateTimeInput(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (num: number) => String(num).padStart(2, "0");
  const year = date.getFullYear();
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hours = pad(date.getHours());
  const minutes = pad(date.getMinutes());
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function toGoogleDateRange(startInput: string, durationMinutes: number): string | null {
  const start = new Date(startInput);
  if (Number.isNaN(start.getTime())) return null;
  const end = new Date(start.getTime() + Math.max(15, durationMinutes) * 60_000);
  const fmt = (date: Date) =>
    `${date.getUTCFullYear()}${String(date.getUTCMonth() + 1).padStart(2, "0")}${String(date.getUTCDate()).padStart(2, "0")}T${String(
      date.getUTCHours()
    ).padStart(2, "0")}${String(date.getUTCMinutes()).padStart(2, "0")}00Z`;
  return `${fmt(start)}/${fmt(end)}`;
}

function splitEmails(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function extractPatchArtifact(artifacts: TicketArtifacts): PatchArtifactDetail | null {
  const detail = asRecord(artifacts.patch_artifact_detail);
  const fallback = asRecord(artifacts.patch_artifact);
  const source = detail ?? fallback;
  if (!source) return null;

  const diffRaw = source.diff;
  const formatRaw = source.format;
  const changedRaw = source.changed_files;
  const diff = typeof diffRaw === "string" ? diffRaw : "";
  const format = typeof formatRaw === "string" ? formatRaw : "unified_diff";
  const changedFiles = Array.isArray(changedRaw)
    ? changedRaw.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];

  if (!diff.trim()) return null;
  return {
    format,
    diff,
    changed_files: changedFiles
  };
}

function toDisplayRows(lines: DiffRawLine[]): DiffDisplayRow[] {
  const rows: DiffDisplayRow[] = [];
  let idx = 0;

  while (idx < lines.length) {
    const current = lines[idx];

    if (current.kind === "del") {
      const dels: DiffRawLine[] = [];
      while (idx < lines.length && lines[idx].kind === "del") {
        dels.push(lines[idx]);
        idx += 1;
      }

      const adds: DiffRawLine[] = [];
      while (idx < lines.length && lines[idx].kind === "add") {
        adds.push(lines[idx]);
        idx += 1;
      }

      if (adds.length) {
        const max = Math.max(dels.length, adds.length);
        for (let i = 0; i < max; i += 1) {
          const left = dels[i];
          const right = adds[i];
          rows.push({
            rowType: "change",
            oldLine: left?.oldLine ?? null,
            oldText: left?.text ?? "",
            newLine: right?.newLine ?? null,
            newText: right?.text ?? ""
          });
        }
      } else {
        for (const left of dels) {
          rows.push({
            rowType: "del",
            oldLine: left.oldLine,
            oldText: left.text,
            newLine: null,
            newText: ""
          });
        }
      }
      continue;
    }

    if (current.kind === "add") {
      const adds: DiffRawLine[] = [];
      while (idx < lines.length && lines[idx].kind === "add") {
        adds.push(lines[idx]);
        idx += 1;
      }
      for (const right of adds) {
        rows.push({
          rowType: "add",
          oldLine: null,
          oldText: "",
          newLine: right.newLine,
          newText: right.text
        });
      }
      continue;
    }

    if (current.kind === "context") {
      rows.push({
        rowType: "context",
        oldLine: current.oldLine,
        oldText: current.text,
        newLine: current.newLine,
        newText: current.text
      });
      idx += 1;
      continue;
    }

    rows.push({
      rowType: "meta",
      oldLine: null,
      oldText: current.text,
      newLine: null,
      newText: current.text
    });
    idx += 1;
  }

  return rows;
}

function parseUnifiedDiff(diff: string): DiffFile[] | null {
  const lines = diff.split("\n");
  const files: ParsedFile[] = [];

  let currentFile: ParsedFile | null = null;
  let currentHunk: { header: string; lines: DiffRawLine[] } | null = null;
  let oldCursor = 0;
  let newCursor = 0;

  const ensureFile = () => {
    if (currentFile) return;
    currentFile = { oldPath: "(unknown)", newPath: "(unknown)", hunks: [] };
    files.push(currentFile);
  };

  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      currentFile = null;
      currentHunk = null;
      continue;
    }

    if (line.startsWith("--- ")) {
      currentFile = { oldPath: line.slice(4).trim(), newPath: "", hunks: [] };
      files.push(currentFile);
      currentHunk = null;
      continue;
    }

    if (line.startsWith("+++ ")) {
      ensureFile();
      currentFile!.newPath = line.slice(4).trim();
      continue;
    }

    if (line.startsWith("@@")) {
      ensureFile();
      currentHunk = { header: line, lines: [] };
      currentFile!.hunks.push(currentHunk);

      const match = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(line);
      oldCursor = match ? Number(match[1]) : 0;
      newCursor = match ? Number(match[2]) : 0;
      continue;
    }

    if (!currentFile || !currentHunk) {
      continue;
    }

    if (line.startsWith("+") && !line.startsWith("+++")) {
      currentHunk.lines.push({ kind: "add", text: line.slice(1), oldLine: null, newLine: newCursor });
      newCursor += 1;
      continue;
    }

    if (line.startsWith("-") && !line.startsWith("---")) {
      currentHunk.lines.push({ kind: "del", text: line.slice(1), oldLine: oldCursor, newLine: null });
      oldCursor += 1;
      continue;
    }

    if (line.startsWith(" ")) {
      currentHunk.lines.push({ kind: "context", text: line.slice(1), oldLine: oldCursor, newLine: newCursor });
      oldCursor += 1;
      newCursor += 1;
      continue;
    }

    currentHunk.lines.push({ kind: "meta", text: line, oldLine: null, newLine: null });
  }

  const normalized = files
    .map<DiffFile>((file) => ({
      oldPath: file.oldPath || "(unknown)",
      newPath: file.newPath || "(unknown)",
      hunks: file.hunks.map((hunk) => ({
        header: hunk.header,
        rows: toDisplayRows(hunk.lines)
      }))
    }))
    .filter((file) => file.hunks.length > 0);

  return normalized.length ? normalized : null;
}

function rowClassName(row: DiffDisplayRow): string {
  return `side-diff-row side-diff-${row.rowType}`;
}

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
  const [emailTo, setEmailTo] = useState("");
  const [emailSubject, setEmailSubject] = useState("");
  const [emailBody, setEmailBody] = useState("");
  const [meetingAttendees, setMeetingAttendees] = useState("");
  const [meetingObjective, setMeetingObjective] = useState("Clarify missing details and next actions");
  const [meetingDuration, setMeetingDuration] = useState("30");
  const [meetingStart, setMeetingStart] = useState("");
  const [manualActionMessage, setManualActionMessage] = useState<string | null>(null);
  const [manualBusy, setManualBusy] = useState(false);
  const [isDiffFullscreen, setIsDiffFullscreen] = useState(false);
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

  const patchArtifact = useMemo(() => (story ? extractPatchArtifact(story.artifacts) : null), [story]);
  const parsedDiff = useMemo(() => (patchArtifact ? parseUnifiedDiff(patchArtifact.diff) : null), [patchArtifact]);
  const diffStats = useMemo(() => {
    if (!parsedDiff) return { add: 0, del: 0, change: 0, context: 0 };
    let add = 0;
    let del = 0;
    let change = 0;
    let context = 0;
    for (const file of parsedDiff) {
      for (const hunk of file.hunks) {
        for (const row of hunk.rows) {
          if (row.rowType === "add") add += 1;
          else if (row.rowType === "del") del += 1;
          else if (row.rowType === "change") change += 1;
          else if (row.rowType === "context") context += 1;
        }
      }
    }
    return { add, del, change, context };
  }, [parsedDiff]);
  const suggestedSlots = useMemo(
    () => (story ? toStringArray((story.artifacts as Record<string, unknown>).calendar_slots) : []),
    [story]
  );
  const suggestedEmailSubject = useMemo(
    () => (story && typeof story.artifacts.email_subject === "string" ? story.artifacts.email_subject : ""),
    [story]
  );

  useEffect(() => {
    if (!story) return;
    setEmailSubject((prev) => prev || suggestedEmailSubject || `[${story.ticket_id}] Follow-up required`);
    setEmailBody((prev) =>
      prev ||
      `Ticket: ${story.ticket_id}\nSummary: ${story.summary}\n\nPlease share additional details so we can proceed.`
    );
    if (!meetingStart && suggestedSlots.length > 0) {
      const first = toLocalDateTimeInput(suggestedSlots[0]);
      if (first) {
        setMeetingStart(first);
      }
    }
  }, [story, suggestedEmailSubject, suggestedSlots, meetingStart]);

  useEffect(() => {
    if (patchArtifact) return;
    setIsDiffFullscreen(false);
  }, [patchArtifact]);

  useEffect(() => {
    if (!isDiffFullscreen) return;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsDiffFullscreen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [isDiffFullscreen]);

  const onManualEmailLog = async () => {
    if (!ticketId) return;
    setManualBusy(true);
    setManualActionMessage(null);
    try {
      await addStoryEvent(ticketId, {
        kind: "EMAIL_DRAFTED",
        source: "MANUAL",
        actor,
        team,
        payload: {
          to: splitEmails(emailTo),
          subject: emailSubject,
          body: emailBody,
          mode: "manual_ui"
        }
      });
      setManualActionMessage("Manual email draft logged to ticket timeline.");
      load(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setManualBusy(false);
    }
  };

  const onOpenMailto = () => {
    const to = splitEmails(emailTo).join(",");
    const url = `mailto:${to}?subject=${encodeURIComponent(emailSubject)}&body=${encodeURIComponent(emailBody)}`;
    window.open(url, "_blank", "noopener,noreferrer");
  };

  const onManualMeetingLog = async () => {
    if (!ticketId) return;
    setManualBusy(true);
    setManualActionMessage(null);
    const duration = Number.parseInt(meetingDuration, 10);
    const parsedDuration = Number.isFinite(duration) ? Math.max(15, Math.min(duration, 90)) : 30;
    const parsedStart = meetingStart ? new Date(meetingStart) : null;
    const startIso = parsedStart && !Number.isNaN(parsedStart.getTime()) ? parsedStart.toISOString() : "";
    try {
      await addStoryEvent(ticketId, {
        kind: "MEETING_SCHEDULED",
        source: "MANUAL",
        actor,
        team,
        payload: {
          attendees: splitEmails(meetingAttendees),
          objective: meetingObjective,
          duration_minutes: parsedDuration,
          start_time: startIso || null,
          mode: "manual_ui"
        }
      });
      setManualActionMessage("Manual calendar meeting logged to ticket timeline.");
      load(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setManualBusy(false);
    }
  };

  const onOpenCalendar = () => {
    const duration = Number.parseInt(meetingDuration, 10);
    const parsedDuration = Number.isFinite(duration) ? Math.max(15, Math.min(duration, 90)) : 30;
    const title = `[${story?.ticket_id || "Ticket"}] Follow-up sync`;
    const details = `${meetingObjective}\n\n${emailBody}`;
    const attendees = splitEmails(meetingAttendees).join(",");
    const dateRange = meetingStart ? toGoogleDateRange(meetingStart, parsedDuration) : null;
    const base = "https://calendar.google.com/calendar/u/0/r/eventedit";
    const params = new URLSearchParams();
    params.set("text", title);
    params.set("details", details);
    if (attendees) {
      params.set("add", attendees);
    }
    if (dateRange) {
      params.set("dates", dateRange);
    }
    window.open(`${base}?${params.toString()}`, "_blank", "noopener,noreferrer");
  };

  if (!ticketId) return <p className="error-text">Ticket ID is missing.</p>;
  if (loading) return <p>Loading story...</p>;
  if (!story) return <p className="error-text">{error || "Ticket not found"}</p>;

  const renderDiffSections = (fullscreen: boolean) => {
    if (!patchArtifact) {
      return (
        <p className="meta-line">
          No engineer patch yet. It appears here after manager selects READY_TO_PATCH and engineer runs.
        </p>
      );
    }

    return (
      <>
        <p className="meta-line">Format: {patchArtifact.format}</p>
        <p className="meta-line">
          Changed files: {patchArtifact.changed_files.length ? patchArtifact.changed_files.join(", ") : "n/a"}
        </p>
        <p className="meta-line">
          Changes: +{diffStats.add} / -{diffStats.del}
          {diffStats.change ? ` / ~${diffStats.change}` : ""}
        </p>

        {parsedDiff ? (
          <div className="side-diff-container">
            {parsedDiff.map((file, fileIdx) => (
              <section key={`${file.oldPath}-${file.newPath}-${fileIdx}`} className="side-diff-file-block">
                <div className="side-diff-file-meta mono">
                  <span>{file.oldPath}</span>
                  <span className="mono">→</span>
                  <span>{file.newPath}</span>
                </div>
                <div className={`side-diff-scroll${fullscreen ? " side-diff-scroll-fullscreen" : ""}`}>
                  <div className="side-diff-grid" role="table" aria-label="Side-by-side diff">
                    <div className="side-diff-grid-head" role="row">
                      <div role="columnheader">Old</div>
                      <div role="columnheader">New</div>
                    </div>
                    <div className="side-diff-grid-body" role="rowgroup">
                      {file.hunks.map((hunk, hunkIdx) => (
                        <Fragment key={`${fileIdx}-hunk-${hunkIdx}`}>
                          <div className="side-diff-hunk-row" role="row">
                            <div className="side-diff-hunk-cell" role="cell">{hunk.header}</div>
                          </div>
                          {hunk.rows.map((row, rowIdx) => {
                            const isAdd = row.rowType === "add";
                            const isDel = row.rowType === "del";
                            const oldLine = isAdd ? "" : row.oldLine ?? "";
                            const oldText = isAdd ? " " : row.oldText || " ";
                            const newLine = isDel ? "" : row.newLine ?? "";
                            const newText = isDel ? " " : row.newText || " ";
                            return (
                              <div key={`${fileIdx}-${hunkIdx}-${rowIdx}`} className={rowClassName(row)} role="row">
                                <div className="side-diff-cell" role="cell">
                                  <span className="side-diff-line-number mono">{oldLine}</span>
                                  <span className="side-diff-code mono">{oldText}</span>
                                </div>
                                <div className="side-diff-cell" role="cell">
                                  <span className="side-diff-line-number mono">{newLine}</span>
                                  <span className="side-diff-code mono">{newText}</span>
                                </div>
                              </div>
                            );
                          })}
                        </Fragment>
                      ))}
                    </div>
                  </div>
                </div>
              </section>
            ))}
          </div>
        ) : (
          <div className="raw-diff-fallback">
            <p className="meta-line">Diff parser fallback: rendering raw unified diff.</p>
            <pre className={`raw-diff${fullscreen ? " raw-diff-fullscreen" : ""}`}>{patchArtifact.diff}</pre>
          </div>
        )}
      </>
    );
  };

  return (
    <section>
      <header className="page-header">
        <h2>{story.ticket_id}</h2>
        <p>{story.summary}</p>
        <p className="meta-line">
          Live stream: <span className={`stream-badge stream-${streamStatus}`}>{streamStatus.toUpperCase()}</span>
        </p>
        {streamError ? <p className="error-text">{streamError}</p> : null}
      </header>

      <FlowLine flow={flow} />

      <div className="story-layout">
        <div className="card story-main-card">
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

        <aside className="stack story-right-scroll">

          <div className="card story-quick-actions">
            <div>
              <h4>Engineer Diff Review</h4>
              {patchArtifact ? (
                <p className="meta-line">
                  {patchArtifact.changed_files.length
                    ? `${patchArtifact.changed_files.length} changed file${patchArtifact.changed_files.length > 1 ? "s" : ""}`
                    : "Patch artifact available"}
                </p>
              ) : (
                <p className="meta-line">No patch available for this ticket yet.</p>
              )}
            </div>
            <button className="btn primary" type="button" disabled={!patchArtifact} onClick={() => setIsDiffFullscreen(true)}>
              Open Diff Fullscreen
            </button>
          </div>

          <div className="card ops-card">
            <div className="ops-header">
              <h3>Ticket Operations</h3>
              <p className="meta-line">Manual controls for comms, scheduling, and approvals.</p>
            </div>

            <section className="ops-section">
              <h4>Email Draft</h4>
              <label className="label">To (comma separated)</label>
              <input
                className="input"
                value={emailTo}
                onChange={(e) => setEmailTo(e.target.value)}
                placeholder="qa@example.com, manager@example.com"
              />
              <label className="label">Subject</label>
              <input className="input" value={emailSubject} onChange={(e) => setEmailSubject(e.target.value)} />
              <label className="label">Body</label>
              <textarea className="input" value={emailBody} onChange={(e) => setEmailBody(e.target.value)} />
              <div className="manual-actions">
                <button className="btn subtle" type="button" disabled={manualBusy} onClick={onManualEmailLog}>
                  Log Email Draft
                </button>
                <button className="btn primary" type="button" onClick={onOpenMailto}>
                  Open Mail App
                </button>
              </div>
            </section>

            <section className="ops-section">
              <h4>Calendar Meeting</h4>
              {suggestedSlots.length ? (
                <div className="slot-suggestions">
                  {suggestedSlots.slice(0, 3).map((slot) => (
                    <button
                      key={slot}
                      className="pill slot-btn"
                      type="button"
                      onClick={() => setMeetingStart(toLocalDateTimeInput(slot))}
                    >
                      {new Date(slot).toLocaleString()}
                    </button>
                  ))}
                </div>
              ) : null}
              <label className="label">Attendees (comma separated)</label>
              <input
                className="input"
                value={meetingAttendees}
                onChange={(e) => setMeetingAttendees(e.target.value)}
                placeholder="eng@example.com, pm@example.com"
              />
              <label className="label">Objective</label>
              <input className="input" value={meetingObjective} onChange={(e) => setMeetingObjective(e.target.value)} />
              <div className="manual-comms-inline">
                <div>
                  <label className="label">Start time</label>
                  <input className="input" type="datetime-local" value={meetingStart} onChange={(e) => setMeetingStart(e.target.value)} />
                </div>
                <div>
                  <label className="label">Duration (min)</label>
                  <input className="input" value={meetingDuration} onChange={(e) => setMeetingDuration(e.target.value)} />
                </div>
              </div>
              <div className="manual-actions">
                <button className="btn subtle" type="button" disabled={manualBusy} onClick={onManualMeetingLog}>
                  Log Meeting Event
                </button>
                <button className="btn accent" type="button" onClick={onOpenCalendar}>
                  Open Calendar
                </button>
              </div>
            </section>

            <section className="ops-section">
              <h4>Manual Timeline Event</h4>
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
                <button className="btn subtle" type="submit">
                  Add Event
                </button>
              </form>
            </section>

            <section className="ops-section">
              <h4>Approval Control</h4>
              <label className="label">Reviewer</label>
              <input className="input" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
              <label className="label">Comment</label>
              <textarea className="input" value={approveNote} onChange={(e) => setApproveNote(e.target.value)} />
              <button className="btn accent" onClick={onApprove}>
                Approve Ticket Flow
              </button>
            </section>

            {manualActionMessage ? <p className="meta-line">{manualActionMessage}</p> : null}
          </div>

          <div className="card diff-card">
            <div className="diff-card-header">
              <h3>Engineer Patch Diff</h3>
              {patchArtifact ? (
                <button className="btn subtle diff-expand-btn" type="button" onClick={() => setIsDiffFullscreen(true)}>
                  Fullscreen
                </button>
              ) : null}
            </div>
            {renderDiffSections(false)}
            <details className="artifact-details">
              <summary>View Raw Artifacts</summary>
              <pre>{JSON.stringify(story.artifacts, null, 2)}</pre>
            </details>
          </div>

          <div className="card visually-muted">
            <h3>Context</h3>
            <p className="meta-line">Ticket ID: <span className="mono">{story.ticket_id}</span></p>
            <p className="meta-line">Status: {story.status}</p>
            <p className="meta-line">Assignee: {story.assignee || "unassigned"}</p>
          </div>
        </aside>
      </div>
      {isDiffFullscreen && patchArtifact ? (
        <div
          className="diff-fullscreen-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Engineer patch diff fullscreen"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setIsDiffFullscreen(false);
            }
          }}
        >
          <section className="diff-fullscreen-panel">
            <header className="diff-fullscreen-header">
              <div>
                <h3>Engineer Patch Diff</h3>
                <p className="meta-line">
                  Side-by-side review mode. Press <span className="mono">Esc</span> to close.
                </p>
              </div>
              <button className="btn accent" type="button" onClick={() => setIsDiffFullscreen(false)}>
                Close Fullscreen
              </button>
            </header>
            <div className="diff-fullscreen-body">{renderDiffSections(true)}</div>
          </section>
        </div>
      ) : null}
      {error && <p className="error-text">{error}</p>}
    </section>
  );
}
