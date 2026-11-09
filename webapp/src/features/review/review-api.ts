import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { ChangeRow, DiffPayload, Task, TaskGitInfo } from "../../api/types";

export interface WorkspaceFile {
  rel_path: string;
  action: string;
  size: number;
}

export interface WorkspacePending {
  count: number;
  files: WorkspaceFile[];
  truncated?: boolean;
}

export type MergeStatus =
  | "merged"
  | "conflict"
  | "blocked"
  | "discarded"
  | "missing_branch"
  | "auto_merge_disabled"
  | "not_merged"
  | null;

export interface TaskWorkspace {
  task_id: string;
  project_id: string;
  isolation: string;
  root_path: string;
  workdir: string;
  workdir_exists: boolean;
  is_original: boolean;
  running: boolean;
  can_sync: boolean;
  sync_blocked_reason?: string | null;
  pending?: WorkspacePending;
  unsynced?: WorkspacePending;
  /** ``task.result.git``（copy/shared 或非 git 项目为 null）。 */
  git?: TaskGitInfo | null;
  /** 后端归纳的合并状态；null 表示不涉及 git，仍按文件同步语义处理。 */
  merge_status?: MergeStatus;
}

export interface SyncResult {
  task_id: string;
  project_id: string;
  dry_run: boolean;
  already_shared: boolean;
  already_synced: number;
  committed: string[];
  skipped: string[];
  skipped_details: Array<{ rel_path?: string; reason?: string }>;
  conflicts: string[];
  out_of_scope: string[];
  message: string;
}

/** ``POST /api/tasks/{tid}/merge`` 的成功响应（git 分支合并或文件回收）。 */
export interface MergeResult {
  merged?: boolean;
  blocked?: boolean;
  conflict?: boolean;
  reason?: string | null;
  mode?: string;
  branch?: string | null;
  base?: string | null;
  sha?: string | null;
  files?: string[];
  count?: number;
  committed?: string[];
  message?: string;
}

export interface DiscardResult {
  discarded: boolean;
  branch_deleted: boolean;
  branch?: string | null;
  message: string;
}

export interface RebuildBranchResult {
  rebuilt: boolean;
  branch: string;
  base: string;
  sha: string;
}

export type ReviewGroupKey = "review" | "synced" | "failed";

export interface ReviewEntry {
  task: Task;
  changes: ChangeRow[];
  workspace?: TaskWorkspace;
  workspaceLoading: boolean;
  workspaceError: boolean;
  group: ReviewGroupKey;
}

export const REVIEWABLE_STATUSES = new Set<Task["status"]>([
  "success",
  "failed",
  "blocked",
  "waiting",
]);

function createLimiter(max: number) {
  let active = 0;
  const waiters: Array<() => void> = [];

  return async function limit<T>(fn: () => Promise<T>): Promise<T> {
    if (active >= max) {
      await new Promise<void>((resolve) => waiters.push(resolve));
    }
    active += 1;
    try {
      return await fn();
    } finally {
      active -= 1;
      const next = waiters.shift();
      next?.();
    }
  };
}

const changeLimit = createLimiter(4);
const workspaceLimit = createLimiter(2);
const diffLimit = createLimiter(3);

export const reviewKeys = {
  changes: (tid: string) => ["review", "changes", tid] as const,
  workspace: (tid: string) => ["review", "workspace", tid] as const,
  diff: (tid: string, path: string) => ["review", "diff", tid, path] as const,
};

export function taskChangesQuery(tid: string) {
  return {
    queryKey: reviewKeys.changes(tid),
    queryFn: () =>
      changeLimit(() => api.get<ChangeRow[]>(`/api/tasks/${encodeURIComponent(tid)}/changes`)),
    staleTime: 15_000,
    retry: 1,
  };
}

export function taskWorkspaceQuery(tid: string) {
  return {
    queryKey: reviewKeys.workspace(tid),
    queryFn: () =>
      workspaceLimit(() => api.get<TaskWorkspace>(`/api/tasks/${encodeURIComponent(tid)}/workspace`)),
    staleTime: 20_000,
    retry: false,
  };
}

export function taskDiffQuery(tid: string, path: string) {
  return {
    queryKey: reviewKeys.diff(tid, path),
    queryFn: () =>
      diffLimit(() =>
        api.get<DiffPayload>(
          `/api/tasks/${encodeURIComponent(tid)}/diff?path=${encodeURIComponent(path)}`,
        ),
      ),
    staleTime: 60_000,
    retry: false,
  };
}

export function useTaskDiff(tid: string, path: string | null, enabled = true) {
  return useQuery({
    ...taskDiffQuery(tid, path ?? ""),
    enabled: Boolean(tid && path) && enabled,
  });
}

export function useSyncTask(tid: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<SyncResult>(`/api/tasks/${encodeURIComponent(tid)}/sync`, {}),
    onSuccess: () => {
      invalidateReview(client, tid);
    },
  });
}

function invalidateReview(client: ReturnType<typeof useQueryClient>, tid: string) {
  void client.invalidateQueries({ queryKey: reviewKeys.changes(tid) });
  void client.invalidateQueries({ queryKey: reviewKeys.workspace(tid) });
  void client.invalidateQueries({ queryKey: ["review", "diff", tid] });
  void client.invalidateQueries({ queryKey: ["dashboard"] });
  void client.invalidateQueries({ queryKey: ["task", tid] });
}

/**
 * 人工把任务分支合并回基线（worktree / git 任务）。
 *
 * 冲突 / 脏树阻断会返回 409，响应体是顶层 JSON（conflict / blocked / files /
 * dirty_files / branch / base / message），被 ``api.post`` 包进 ``ApiError.body``；
 * 调用方据此更新内联面板，**不自动解决冲突**。
 */
export function useMergeTask(tid: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<MergeResult>(`/api/tasks/${encodeURIComponent(tid)}/merge`, {}),
    onSuccess: () => {
      invalidateReview(client, tid);
    },
  });
}

/** 放弃任务分支：删除工作副本并强制删除分支（未合并内容不可找回）。 */
export function useDiscardBranch(tid: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<DiscardResult>(
        `/api/tasks/${encodeURIComponent(tid)}/discard-worktree`,
        { delete_branch: true },
      ),
    onSuccess: () => {
      invalidateReview(client, tid);
    },
  });
}

/** 任务分支 ref 被删除后，按记录的 commit/base 重建分支与工作副本。 */
export function useRebuildBranch(tid: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<RebuildBranchResult>(
        `/api/tasks/${encodeURIComponent(tid)}/rebuild-branch`,
        { confirm: true },
      ),
    onSuccess: () => {
      invalidateReview(client, tid);
    },
  });
}

export function latestChanges(rows: ChangeRow[] | undefined): ChangeRow[] {
  const byPath = new Map<string, ChangeRow>();
  for (const row of rows ?? []) {
    const previous = byPath.get(row.rel_path);
    if (!previous || row.id >= previous.id) byPath.set(row.rel_path, row);
  }
  return [...byPath.values()].sort((left, right) =>
    left.rel_path.localeCompare(right.rel_path, "en"),
  );
}

export function groupFor(task: Task, workspace?: TaskWorkspace): ReviewGroupKey {
  if (workspace?.merge_status === "merged") return "synced";
  if (workspace?.merge_status === "discarded") return "failed";
  if (
    workspace?.merge_status === "conflict" ||
    workspace?.merge_status === "blocked" ||
    workspace?.merge_status === "missing_branch"
  ) {
    return "review";
  }
  if (task.status !== "success") return "failed";
  if (!workspace) return "review";
  if (workspace.is_original) return "synced";
  return (workspace.unsynced?.count ?? 0) === 0 ? "synced" : "review";
}

export function splitPath(path: string): { name: string; dir: string } {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  const name = parts.pop() ?? path;
  return { name, dir: parts.join("/") };
}

export function changeSize(change: ChangeRow): number {
  return change.action === "delete"
    ? change.before_size ?? 0
    : change.after_size ?? change.before_size ?? 0;
}

export const ACTION_META: Record<
  string,
  { badge: string; label: string; tone: "success" | "warning" | "danger" | "default" }
> = {
  add: { badge: "+", label: "新增", tone: "success" },
  create: { badge: "+", label: "新增", tone: "success" },
  modify: { badge: "~", label: "改动", tone: "warning" },
  update: { badge: "~", label: "改动", tone: "warning" },
  delete: { badge: "-", label: "删除", tone: "danger" },
};

export function actionMeta(action: string) {
  return ACTION_META[action] ?? { badge: "?", label: action || "未知", tone: "default" as const };
}
