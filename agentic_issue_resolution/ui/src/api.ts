import type {
  ABRunRecord,
  FlowSnapshot,
  LiveDecision,
  MetricsSummary,
  ModelOpsSummary,
  OperateRunDetail,
  OperateRunResponse,
  SelectorResponse,
  TicketListItem,
  TicketStory
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE || "";

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {})
    },
    ...init
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  return (await response.json()) as T;
}

export async function listTickets(): Promise<TicketListItem[]> {
  const data = await fetchJson<{ items: TicketListItem[] }>("/tickets");
  return data.items;
}

export function getTicketStory(ticketId: string): Promise<TicketStory> {
  return fetchJson<TicketStory>(`/ticket/${ticketId}/story`);
}

export function addStoryEvent(ticketId: string, payload: Record<string, unknown>) {
  return fetchJson(`/ticket/${ticketId}/story-events`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function approveTicket(ticketId: string, reviewer: string, comments: string) {
  return fetchJson("/approve", {
    method: "POST",
    body: JSON.stringify({
      ticket_id: ticketId,
      reviewer,
      approved: true,
      comments
    })
  });
}

export function runAB(payload: Record<string, unknown>): Promise<OperateRunResponse> {
  return fetchJson<OperateRunResponse>("/operate/ab_run", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function judgeAB(payload: Record<string, unknown>) {
  return fetchJson("/operate/judge", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function getOperateRun(runId: string): Promise<OperateRunDetail> {
  return fetchJson<OperateRunDetail>(`/operate/ab_run/${runId}`);
}

export function getSelector(minSamples = 10): Promise<SelectorResponse> {
  return fetchJson<SelectorResponse>(`/operate/selector?min_samples=${minSamples}`);
}

export async function listABRuns(limit = 20): Promise<ABRunRecord[]> {
  const data = await fetchJson<{ items: ABRunRecord[] }>(`/operate/ab_runs?limit=${limit}`);
  return data.items;
}

export async function listLiveDecisions(limit = 100): Promise<LiveDecision[]> {
  const data = await fetchJson<{ items: LiveDecision[] }>(`/operate/live_decisions?limit=${limit}`);
  return data.items;
}

export function getMetricsSummary(): Promise<MetricsSummary> {
  return fetchJson<MetricsSummary>("/metrics/summary");
}

export function getModelOpsSummary(): Promise<ModelOpsSummary> {
  return fetchJson<ModelOpsSummary>("/metrics/model_ops");
}

export function getFlow(ticketId: string): Promise<FlowSnapshot> {
  return fetchJson<FlowSnapshot>(`/flow/${ticketId}`);
}

export function openFlowStream(
  ticketId: string,
  onFlow: (flow: FlowSnapshot) => void,
  onError?: (message: string) => void
): EventSource {
  const source = new EventSource(`${API_BASE}/stream/${ticketId}`);
  source.addEventListener("flow_update", (event) => {
    try {
      const payload = JSON.parse((event as MessageEvent).data) as {
        type: string;
        flow: FlowSnapshot;
      };
      if (payload.flow) {
        onFlow(payload.flow);
      }
    } catch (err) {
      onError?.((err as Error).message);
    }
  });
  source.onerror = () => {
    onError?.("flow stream disconnected");
  };
  return source;
}
