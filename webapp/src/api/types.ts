export type TaskStatus =
  | "pending"
  | "running"
  | "success"
  | "failed"
  | "blocked"
  | "waiting"
  | "skipped"
  | "cancelled"
  | "interrupted";

export interface Project {
  id: string;
  name: string;
  label?: string | null;
  root_path: string;
  readonly?: boolean;
  created_at?: string;
}

export interface MergeInfo {
  merged?: boolean;
  conflict?: boolean;
  blocked?: boolean;
  skipped?: boolean;
  discarded?: boolean;
  reason?: string | null;
  sha?: string | null;
  branch?: string | null;
  base?: string | null;
  files?: string[];
  dirty_files?: string[];
  error?: string | null;
  source?: "auto" | "manual" | string;
}

export interface TaskGitInfo {
  branch?: string | null;
  commit?: string | null;
  base?: string | null;
  files?: string[];
  auto_merge?: boolean;
  merge?: MergeInfo | null;
  worktree_removed?: boolean;
  worktree_path?: string | null;
  worktree_remove_error?: string | null;
  error?: string | null;
}

export interface TaskCostInfo {
  tokens?: number;
  amount?: number;
  source?: "reported" | "estimated" | "unreported" | string;
}

export interface TaskIsolationInfo {
  requested_mode?: string;
  mode?: string;
  workdir?: string;
  degraded?: boolean;
  degrade_code?: string | null;
  degrade_reason?: string | null;
}

export interface TaskProcessInfo {
  started?: Array<{ pid: number; port?: number | null; command?: string }>;
  reaped?: Array<{ pid: number; port?: number | null }>;
  warnings?: string[];
}

export interface TaskResult {
  git?: TaskGitInfo;
  cost?: TaskCostInfo;
  isolation?: TaskIsolationInfo;
  processes?: TaskProcessInfo;
  [key: string]: unknown;
}

export interface Task {
  id: string;
  project_id: string;
  parent_id?: string | null;
  title: string;
  kind: string;
  brief: string;
  tags: string[];
  role: string;
  template_id?: string | null;
  agent_id?: string | null;
  status: TaskStatus;
  priority: number;
  attempt: number;
  max_attempts: number;
  runtime?: {
    attempt?: number;
    max_attempts?: number;
    base_timeout_sec?: number;
    effective_timeout_sec?: number;
    next_attempt_timeout_sec?: number;
    elapsed_sec?: number;
    remaining_sec?: number;
    warning?: boolean;
    warning_text?: string;
  };
  timeout_sec?: number | null;
  isolation: "shared" | "copy" | "worktree";
  read_only: boolean;
  write_scope: string[];
  acceptance: unknown[];
  acceptance_ref?: string | null;
  inputs: string[];
  outputs: string[];
  workdir?: string | null;
  blocked_reason?: string | null;
  last_error?: string | null;
  result: TaskResult;
  ordering: number;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface AgentUsage {
  agent_id?: string;
  id?: string;
  label?: string;
  invocations?: number;
  seconds?: number;
  cost?: number;
  tokens?: number;
  limits?: Record<string, number>;
  quota?: Record<string, number>;
  usage?: Record<string, number>;
  enabled?: boolean;
  protocol?: string;
  model?: string | null;
  roles?: string[];
  strengths?: string[];
  can_write?: boolean;
}

export interface Run {
  id: string;
  project_id: string;
  task_id: string;
  agent_id: string;
  agent_label?: string;
  protocol?: string;
  role?: string;
  attempt: number;
  timeout_sec: number;
  status: string;
  exit_code: number | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number;
  cost_tokens: number;
  cost_amount: number;
  error: string | null;
  cwd?: string | null;
  transcript_path?: string | null;
  stdout_path?: string | null;
  stderr_path?: string | null;
}

export interface ChangeRow {
  id: number;
  task_id: string;
  run_id?: string;
  rel_path: string;
  action: string;
  before_hash?: string | null;
  after_hash?: string | null;
  before_size?: number | null;
  after_size?: number | null;
  is_binary?: number;
  created_at?: string;
}

export interface DiffPayload {
  path: string;
  binary: boolean;
  action?: string;
  before_size?: number | null;
  after_size?: number | null;
  diff?: string;
  note?: string;
  hunks?: DiffHunk[];
}

export interface DiffHunk {
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  header: string;
  lines: Array<{ type: "context" | "add" | "del"; text: string; old_no?: number; new_no?: number }>;
}

export interface Artifact {
  id: string;
  task_id: string;
  run_id?: string;
  kind: string;
  role?: string;
  rel_path?: string;
  abs_path?: string;
  size?: number;
  sha256?: string;
  created_at?: string;
}

export interface Handoff {
  id: string;
  task_id: string;
  did?: string;
  did_not?: string;
  issues?: string;
  next_steps?: string;
  notes?: string;
  created_at?: string;
}

export interface Approval {
  id: string;
  project_id: string;
  task_id?: string | null;
  run_id?: string | null;
  agent_id?: string | null;
  kind: string;
  title: string;
  detail?: Record<string, unknown>;
  risk?: string;
  status: string;
  created_at?: string;
}

export interface ActivityEvent {
  id?: number;
  kind: string;
  message?: string;
  summary?: string;
  actor?: string;
  at?: string;
  task_id?: string | null;
  agent_id?: string | null;
  created_at?: string;
  data?: Record<string, unknown>;
}

export interface Dashboard {
  project: Project;
  progress: {
    total: number;
    percent: number;
    counters: Record<string, number>;
  };
  tasks: Task[];
  agents: AgentUsage[];
  approvals: Approval[];
  stats: Record<string, unknown>;
  activity: ActivityEvent[];
  running: boolean;
  workspace: { path?: string; bytes?: number; files?: number; truncated?: boolean };
  server_time: string;
}

export interface TaskDetail {
  task: Task;
  dependencies: Task[];
  history: Array<{ status: string; at: string; reason?: string }>;
  runs: Run[];
  artifacts: Artifact[];
  handoff?: Handoff | null;
  changes: ChangeRow[];
}

export interface ProjectFile {
  path: string;
  name: string;
  dir: boolean;
  size: number;
  mtime: number;
}

export interface FileTreeResponse {
  root: string;
  entries: ProjectFile[];
  truncated?: boolean;
}

export interface ValidationCheck {
  id: string;
  label: string;
  command: string;
  ok?: boolean;
  exit_code?: number | null;
  duration_ms?: number;
  output?: string;
  skipped?: boolean;
}

export interface ValidationReport {
  task_id: string;
  ok: boolean;
  checks: ValidationCheck[];
  generated_at: string;
}

export interface StreamEvent {
  id?: number;
  run_id?: string;
  task_id?: string;
  agent_id?: string;
  type: string;
  text?: string;
  at?: string;
  data?: Record<string, unknown>;
}

export interface GitInfo {
  is_repo: boolean;
  root?: string;
  branch?: string;
  head?: string;
  dirty?: boolean;
  detached?: boolean;
  ahead?: number;
  behind?: number;
  remote?: string | null;
}

export interface TaskWorktree {
  task_id: string;
  mode: "worktree" | "copy" | "shared";
  path: string;
  branch?: string | null;
  base_ref?: string | null;
  head?: string | null;
  dirty?: boolean;
  pending_files?: number;
}
