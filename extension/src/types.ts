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
