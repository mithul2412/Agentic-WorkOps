export type TicketListItem = {
  ticket_id: string;
  run_id: string;
  status: string;
  summary: string;
  risk_tier?: string | null;
  current_step: string;
  updated_at: string;
  assignee?: string | null;
};

export type StoryEvent = {
  event_id: string;
  ticket_id: string;
  ts: string;
  kind: string;
  source: string;
  actor?: string | null;
  team?: string | null;
  payload: Record<string, unknown>;
  deleted: boolean;
};

export type PatchArtifactDetail = {
  format: string;
  diff: string;
  changed_files: string[];
};

export type TicketArtifacts = Record<string, unknown> & {
  patch_artifact_detail?: PatchArtifactDetail | null;
  patch_artifact?: Record<string, unknown> | boolean | null;
};

export type TicketStory = {
  ticket_id: string;
  run_id: string;
  status: string;
  summary: string;
  description: string;
  risk_tier?: string | null;
  assignee?: string | null;
  artifacts: TicketArtifacts;
  timeline: StoryEvent[];
};

export type OperateRunResponse = {
  ab_run_id: string;
  total_tasks: number;
  completed_tasks: number;
  run_status: string;
  summary_by_policy: Record<string, Record<string, number>>;
};

export type OperateRunDetail = {
  ab_run: Record<string, unknown>;
  items: Array<Record<string, unknown>>;
  judgments: Array<Record<string, unknown>>;
};

export type SelectorResponse = {
  category_key: string;
  min_samples: number;
  default_policy_id: string;
  epsilon: number;
  rows: Array<{
    category: string;
    policy_id: string;
    wins: number;
    losses: number;
    ties: number;
    total: number;
    win_rate: number;
    best_policy: boolean;
  }>;
};

export type ABRunRecord = {
  ab_run_id: string;
  source: string;
  policy_a_id: string;
  policy_b_id: string;
  status: string;
  total_tasks: number;
  completed_tasks: number;
  created_at: string;
  finished_at?: string | null;
};

export type LiveDecision = {
  decision_id: string;
  ticket_id: string;
  run_id?: string | null;
  team_profile?: string | null;
  category_estimate?: string | null;
  category_actual?: string | null;
  selected_policy_id?: string | null;
  explored: boolean;
  epsilon: number;
  runtime_ms: number;
  cost_proxy: number;
  created_at: string;
};

export type FlowNode = {
  id: string;
  label: string;
  description: string;
  state: "pending" | "active" | "done" | "skipped";
};

export type FlowEdge = {
  source: string;
  target: string;
  condition?: string | null;
};

export type FlowSnapshot = {
  ticket_id: string;
  run_id: string;
  status: string;
  current_step: string;
  updated_at: string;
  timeline_size: number;
  nodes: FlowNode[];
  edges: FlowEdge[];
  last_event?: Record<string, unknown> | null;
};

export type MetricsSummary = {
  generated_at: string;
  totals: {
    tickets: number;
    completed_tickets: number;
  };
  triage_to_fix_cycle_time: {
    avg_minutes: number;
    p50_minutes: number;
    p90_minutes: number;
    sample_size: number;
  };
  reopen_regression_rate: {
    tickets_with_reopen_or_regression: number;
    rate: number;
  };
  handoff_quality: {
    avg_score: number;
    checklist_items: string[];
  };
  policy_win_rate_by_team: Array<{
    team_profile: string;
    best_policy: string;
    win_rate: number;
    sample_size: number;
  }>;
};

export type ModelOpsSummary = {
  generated_at: string;
  manager: {
    decisions: number;
    fallback_decisions: number;
    fallback_rate: number;
  };
  selector: {
    exploration_rate: number;
    explored_count: number;
    total_live_decisions: number;
    win_rate_by_category: Array<{
      category: string;
      best_policy: string;
      win_rate: number;
      sample_size: number;
    }>;
  };
  policy_runtime_cost: Array<{
    policy_id: string;
    provider: string;
    sample_size: number;
    avg_runtime_ms: number;
    avg_cost_proxy: number;
  }>;
  judge_confidence: {
    p50: number;
    p90: number;
    avg: number;
    sample_size: number;
    min_confidence_for_selector: number;
    skipped_low_confidence: number;
    low_confidence_skip_rate: number;
  };
};
