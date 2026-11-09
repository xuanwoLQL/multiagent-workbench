import { useQueries } from "@tanstack/react-query";
import { api, qk } from "../../api/client";
import type { AgentUsage, Dashboard, GitInfo, Project, Task } from "../../api/types";

export type ProjectRecord = Project & {
  updated_at?: string | null;
};

export interface ProjectCardData {
  project: ProjectRecord;
  dashboard: Dashboard | null;
  dashboardLoading: boolean;
  dashboardError: Error | null;
  refetchDashboard: () => void;
  taskCount: number | null;
  runningCount: number;
  successCount: number;
  failedCount: number;
  running: boolean;
  agents: AgentUsage[];
  git: GitInfo | null;
  gitLoading: boolean;
  gitError: Error | null;
  refetchGit: () => void;
  lastActive: string | null;
  lastActiveMs: number;
}

const DASHBOARD_CONCURRENCY = 4;

let dashboardActive = 0;
const dashboardQueue: Array<() => void> = [];

async function withDashboardSlot<T>(run: () => Promise<T>): Promise<T> {
  if (dashboardActive >= DASHBOARD_CONCURRENCY) {
    await new Promise<void>((resolve) => dashboardQueue.push(resolve));
  }
  dashboardActive += 1;
  try {
    return await run();
  } finally {
    dashboardActive -= 1;
    dashboardQueue.shift()?.();
  }
}

function timestamp(value?: string | null): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function maxTimestamp(values: Array<string | null | undefined>): number {
  return values.reduce<number>((max, value) => Math.max(max, timestamp(value)), 0);
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function dashboardTasks(dashboard: Dashboard | null): Task[] {
  return Array.isArray(dashboard?.tasks) ? dashboard.tasks : [];
}

function dashboardAgents(dashboard: Dashboard | null): AgentUsage[] {
  return Array.isArray(dashboard?.agents) ? dashboard.agents : [];
}

function agentInvocations(agent: AgentUsage): number {
  return asNumber(agent.usage?.invocations ?? agent.invocations);
}

function sortAgents(agents: AgentUsage[]): AgentUsage[] {
  return [...agents]
    .filter((agent) => agent.enabled !== false)
    .sort((a, b) => {
      const byUsage = agentInvocations(b) - agentInvocations(a);
      if (byUsage !== 0) return byUsage;
      return String(a.label || a.id || "").localeCompare(String(b.label || b.id || ""), "zh-CN");
    });
}

export function useProjectCards(projects: ProjectRecord[]): ProjectCardData[] {
  const dashboards = useQueries({
    queries: projects.map((project) => ({
      queryKey: qk.dashboard(project.id),
      queryFn: () =>
        withDashboardSlot(() =>
          api.get<Dashboard>(`/api/projects/${project.id}/dashboard`),
        ),
      staleTime: 20_000,
      refetchInterval: 45_000,
      refetchOnWindowFocus: true,
      retry: 1,
    })),
  });

  const gits = useQueries({
    queries: projects.map((project) => ({
      queryKey: qk.git(project.id),
      queryFn: () => api.get<GitInfo>(`/api/projects/${project.id}/git`),
      staleTime: 30_000,
      refetchOnWindowFocus: true,
      retry: false,
    })),
  });

  return projects.map((project, index) => {
    const dashboardQuery = dashboards[index];
    const gitQuery = gits[index];
    const dashboard = dashboardQuery?.data ?? null;
    const git = gitQuery?.data ?? null;
    const tasks = dashboardTasks(dashboard);
    const counters = dashboard?.progress?.counters ?? {};
    const runningCount = asNumber(counters.running);
    const successCount = asNumber(counters.success);
    const failedCount = asNumber(counters.failed) + asNumber(counters.blocked);
    const total = asNumber(dashboard?.progress?.total);
    const taskCount = dashboard
      ? typeof dashboard.progress?.total === "number"
        ? total
        : tasks.length
      : null;
    const activityTimes = [
      ...tasks.map((task) => task.updated_at),
      ...(Array.isArray(dashboard?.activity)
        ? dashboard.activity.map((event) => event.at || event.created_at)
        : []),
    ];
    const lastActiveMs = maxTimestamp([
      project.updated_at,
      project.created_at,
      ...activityTimes,
    ]);

    return {
      project,
      dashboard,
      dashboardLoading: Boolean(dashboardQuery?.isPending),
      dashboardError: (dashboardQuery?.error as Error | null | undefined) ?? null,
      refetchDashboard: () => {
        void dashboardQuery?.refetch();
      },
      taskCount,
      runningCount,
      successCount,
      failedCount,
      running: Boolean(dashboard?.running) || runningCount > 0,
      agents: sortAgents(dashboardAgents(dashboard)),
      git,
      gitLoading: Boolean(gitQuery?.isPending),
      gitError: (gitQuery?.error as Error | null | undefined) ?? null,
      refetchGit: () => {
        void gitQuery?.refetch();
      },
      lastActive: lastActiveMs ? new Date(lastActiveMs).toISOString() : null,
      lastActiveMs,
    };
  });
}

export function createProject(name: string, rootPath: string): Promise<ProjectRecord> {
  return api.post<ProjectRecord>("/api/projects", {
    name,
    root_path: rootPath,
  });
}

export function importProject(
  rootPath: string,
  name?: string,
): Promise<ProjectRecord> {
  return api.post<ProjectRecord>("/api/projects/import", {
    root_path: rootPath,
    path: rootPath,
    name: name || undefined,
  });
}

export function deleteProject(id: string): Promise<unknown> {
  return api.del(`/api/projects/${id}`);
}

export interface DesktopBridge {
  isDesktop?: boolean;
  pickDirectory?: () => Promise<unknown> | unknown;
  openPath?: (target: string) => Promise<unknown> | unknown;
}

export function desktopBridge(): DesktopBridge | null {
  const candidate = (window as unknown as { multiagentDesktop?: DesktopBridge })
    .multiagentDesktop;
  return candidate && typeof candidate === "object" ? candidate : null;
}

export function normalizePathKey(value: string): string {
  const trimmed = value.trim().replace(/\//g, "\\");
  if (!trimmed) return "";
  const isUnc = trimmed.startsWith("\\\\");
  const collapsed = isUnc
    ? `\\\\${trimmed.slice(2).replace(/\\+/g, "\\")}`
    : trimmed.replace(/\\+/g, "\\");
  return collapsed.replace(/\\+$/, "").toLowerCase();
}

export function absolutePathError(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return "请选择或填写项目目录";
  if (/[\u0000-\u001f]/.test(trimmed)) return "路径包含不可见字符，请重新选择";
  if (trimmed.length > 4096) return "路径过长，请重新选择";
  const absolute =
    /^[A-Za-z]:[\\/]/.test(trimmed) || trimmed.startsWith("/") || trimmed.startsWith("\\\\");
  if (!absolute) return "请填写绝对路径，例如 D:\\work\\project";
  return null;
}

export function folderNameFromPath(value: string): string {
  const parts = value.trim().split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

export function findDuplicateProject(
  projects: ProjectRecord[],
  path: string,
): ProjectRecord | null {
  const key = normalizePathKey(path);
  if (!key) return null;
  return projects.find((project) => normalizePathKey(project.root_path) === key) ?? null;
}

export function findNameConflict(
  projects: ProjectRecord[],
  name: string,
): ProjectRecord | null {
  const key = name.trim().toLowerCase();
  if (!key) return null;
  return (
    projects.find((project) => String(project.name || "").trim().toLowerCase() === key) ??
    null
  );
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return "未知错误，请稍后重试";
}
