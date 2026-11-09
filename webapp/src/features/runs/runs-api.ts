import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, qk } from "../../api/client";
import type { Dashboard, Run, Task, TaskDetail } from "../../api/types";

export interface RunRecord extends Run {
  /** Only present when the list came from the per-task fallback. */
  task_title?: string | null;
}

export interface RunsPayload {
  runs: RunRecord[];
  /** True when /api/projects/{pid}/runs was unavailable and we fell back to tasks. */
  degraded: boolean;
}

export const RUN_STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  success: "成功",
  failed: "失败",
  blocked: "阻塞",
  waiting: "待人工",
  cancelled: "已取消",
  interrupted: "已中断",
};

export function runStatusLabel(status: string | null | undefined): string {
  if (!status) return "未知";
  return RUN_STATUS_LABEL[status] ?? status;
}

function byStartedDesc(a: RunRecord, b: RunRecord): number {
  return String(b.started_at ?? "").localeCompare(String(a.started_at ?? ""));
}

async function fetchFromTasks(pid: string): Promise<RunRecord[]> {
  const dashboard = await api.get<Dashboard>(`/api/projects/${pid}/dashboard`);
  const tasks: Task[] = Array.isArray(dashboard.tasks) ? dashboard.tasks : [];
  const perTask = await Promise.all(
    tasks.map(async (task) => {
      try {
        const detail = await api.get<TaskDetail>(`/api/tasks/${task.id}`);
        return (detail.runs ?? []).map<RunRecord>((run) => ({
          ...run,
          task_title: task.title,
        }));
      } catch {
        return [] as RunRecord[];
      }
    }),
  );
  return perTask.flat().sort(byStartedDesc);
}

/**
 * Project run list. Older builds only expose runs through task detail, so a
 * missing route (404/405) degrades to dashboard tasks + /api/tasks/{tid}.
 */
export async function fetchProjectRuns(pid: string): Promise<RunsPayload> {
  try {
    const raw = await api.get<unknown>(`/api/projects/${pid}/runs?limit=500`);
    const rows = Array.isArray(raw) ? (raw as RunRecord[]) : [];
    return { runs: rows.map((row) => ({ ...row })), degraded: false };
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 0;
    if (status !== 404 && status !== 405) throw error;
    const runs = await fetchFromTasks(pid);
    return { runs, degraded: true };
  }
}

export function useRuns(pid: string | null) {
  return useQuery({
    queryKey: ["runs", pid ?? ""],
    queryFn: () => fetchProjectRuns(pid as string),
    enabled: Boolean(pid),
    staleTime: 2000,
    refetchOnWindowFocus: true,
    refetchInterval: (query) =>
      query.state.data?.runs.some((run) => run.status === "running") ? 3000 : 20000,
  });
}

export interface RunOutputRun {
  id?: string;
  agent_id?: string;
  status?: string;
  exit_code?: number | null;
  duration_ms?: number;
  cost_tokens?: number;
  started_at?: string;
  error?: string | null;
}

export interface RunOutputPayload {
  run?: RunOutputRun;
  prompt?: string;
  stdout?: string;
  stderr?: string;
}

export function useRunOutput(rid: string | null) {
  return useQuery({
    queryKey: ["run-output", rid ?? ""],
    queryFn: () => api.get<RunOutputPayload>(`/api/runs/${rid}/output`),
    enabled: Boolean(rid),
    staleTime: 2000,
    refetchInterval: (query) => (query.state.data?.run?.status === "running" ? 3000 : false),
  });
}

export function useStopProject(pid: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<unknown>(`/api/projects/${pid}/stop`, {}),
    onSuccess: () => {
      if (!pid) return;
      void client.invalidateQueries({ queryKey: ["runs", pid] });
      void client.invalidateQueries({ queryKey: qk.dashboard(pid) });
    },
  });
}
