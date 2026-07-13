// The exact JSON contract the UI (ui/theater.html) consumes. Mirrors the payload
// the Python server returns from scan_agents(), so the same UI renders unchanged.

export interface Agent {
  id: string;
  persona_id: number;
  emoji: string;
  role: string;
  subagent_type: string;
  title?: string;
  status: "running" | "stale" | "aborted" | "done";
  tool: string;
  task: string;
  task_short: string;
  result: string | null;
  start_ms: number | null;
  end_ms: number | null;
  session: string;
  session_full: string;
  cwd: string;
  project: string;
  path?: string; // full absolute working directory (card-title tooltip)
  mtime_ms: number;
  is_session: boolean;
}

export interface Payload {
  agents: Agent[];
  versions: string[];
  tested_version: string;
  unknown_versions: string[];
  skipped: number;
}

export interface ComposerMeta {
  name?: string;
  status?: string;
  created_ms?: number | null;
  updated_ms?: number | null;
  checkpoint_ms?: number | null;
}

// Per-model cost/token breakdown for the current billing cycle (team accounts).
export interface UsageModelRow {
  model: string;
  costCents: number;
  requests: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
}

// Live Cursor usage, parsed in the extension host and pushed to the webview.
// Discriminated on `state`; the "ok" fields are only present when state === "ok".
// The session cookie is NEVER included here - only derived numbers cross to the UI.
export interface UsageData {
  state: "ok" | "needsAuth" | "error";
  fetchedAtMs: number;
  error?: string;

  email?: string;
  plan?: string; // membershipType, e.g. "pro" / "enterprise"
  isTeam?: boolean;

  // Included-request usage (dashboard legacy-request logic).
  used?: number;
  limit?: number;
  remaining?: number;
  pct?: number;
  planPercentUsed?: number | null; // raw usage-based % the dashboard shows, if present

  // On-demand / usage-based spend, in dollars.
  onDemandUsed?: number;
  onDemandLimit?: number;
  onDemandRemaining?: number;
  hardLimitPerUser?: number | null;

  // Billing cycle + burn rate.
  cycleStartMs?: number | null;
  cycleEndMs?: number | null;
  daysLeft?: number | null;
  requestsPerDay?: number | null;
  projectedRequests?: number | null;
  projectedToExceed?: boolean;

  perModel?: UsageModelRow[];
  totalCostCents?: number | null;
}
