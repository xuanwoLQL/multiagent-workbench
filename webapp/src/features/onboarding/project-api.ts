import { useQueries, useQuery } from "@tanstack/react-query";
import { ApiError, api, qk } from "../../api/client";
import type { AgentUsage, Dashboard, GitInfo, Project } from "../../api/types";
import { fmtRelative } from "../../lib/format";

export type ProjectRecord = Project & {
  updated_at?: string | null;
  origin_path?: string | null;
};

export type AgentRecord = AgentUsage & {
  command?: string | null;
  error?: string;
};

export interface DesktopBridge {
  isDesktop?: boolean;
  pickDirectory?: () => Promise<unknown> | unknown;
}

export interface ProjectSummary {
  project: ProjectRecord;
  dashboard: Dashboard | null;
  dashboardLoading: boolean;
  taskCount: number | null;
  lastActive: string | null;
  lastActiveMs: number;
}

export function desktopBridge(): DesktopBridge | null {
  const candidate = (window as unknown as { multiagentDesktop?: DesktopBridge }).multiagentDesktop;
  return candidate && typeof candidate === "object" ? candidate : null;
}

/** 仅用于判重，不做真实路径解析。Windows 路径大小写不敏感。 */
export function normalizePathKey(value: string): string {
  const trimmed = value.trim().replace(/\//g, "\\");
  if (!trimmed) return "";
  const isUnc = trimmed.startsWith("\\\\");
  const collapsed = isUnc
    ? `\\\\${trimmed.slice(2).replace(/\\+/g, "\\")}`
    : trimmed.replace(/\\+/g, "\\");
  return collapsed.replace(/\\+$/, "").toLowerCase();
}

export function pathSyntaxError(value: string): string | null {
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
  return projects.find((project) => project.name.trim().toLowerCase() === key) ?? null;
}

function timestamp(value?: string | null): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function maxTimestamp(values: Array<string | null | undefined>): number {
  return values.reduce<number>((max, value) => Math.max(max, timestamp(value)), 0);
}

/** 每个项目读取一次 dashboard，用于任务数与最近活动；失败时安静降级。 */
export function useProjectSummaries(projects: ProjectRecord[]): ProjectSummary[] {
  const queries = useQueries({
    queries: projects.map((project) => ({
      queryKey: qk.dashboard(project.id),
      queryFn: () => api.get<Dashboard>(`/api/projects/${project.id}/dashboard`),
      staleTime: 30_000,
      retry: false,
    })),
  });

  return projects
    .map((project, index) => {
      const query = queries[index];
      const dashboard = query?.data ?? null;
      const activityTimes = [
        ...(dashboard?.tasks ?? []).map((task) => task.updated_at),
        ...(dashboard?.activity ?? []).map((event) => event.at || event.created_at),
      ];
      const lastActiveMs = maxTimestamp([
        project.updated_at,
        project.created_at,
        ...activityTimes,
      ]);
      return {
        project,
        dashboard,
        dashboardLoading: Boolean(query?.isLoading),
        taskCount: dashboard ? dashboard.tasks.length : null,
        lastActive: lastActiveMs ? new Date(lastActiveMs).toISOString() : null,
        lastActiveMs,
      };
    })
    .sort((a, b) => b.lastActiveMs - a.lastActiveMs);
}

export async function importDirectory(
  path: string,
  name?: string,
): Promise<ProjectRecord> {
  const body = { path, name: name || undefined };
  try {
    return await api.post<ProjectRecord>("/api/projects/import", body);
  } catch (error) {
    if (error instanceof ApiError && error.status === 400 && /path/i.test(error.message)) {
      return api.post<ProjectRecord>("/api/projects/import", {
        root_path: path,
        name: name || undefined,
      });
    }
    throw error;
  }
}

export async function createWorkspace(
  path: string,
  name?: string,
): Promise<ProjectRecord> {
  const projectName = name?.trim() || folderNameFromPath(path) || "workspace";
  return api.post<ProjectRecord>("/api/projects", {
    name: projectName,
    label: name?.trim() || projectName,
    root_path: path,
  });
}

export function useAgentsDetail() {
  return useQuery<AgentRecord[]>({
    queryKey: qk.agentDetail,
    queryFn: () => api.get<AgentRecord[]>("/api/agents/detail"),
    retry: 1,
    staleTime: 10_000,
  });
}

export function useProjectGit(pid: string | null) {
  return useQuery<GitInfo>({
    queryKey: qk.git(pid ?? ""),
    queryFn: () => api.get<GitInfo>(`/api/projects/${pid}/git`),
    enabled: Boolean(pid),
    retry: false,
    staleTime: 15_000,
  });
}

export function useProjectDashboard(pid: string | null) {
  return useQuery<Dashboard>({
    queryKey: qk.dashboard(pid ?? ""),
    queryFn: () => api.get<Dashboard>(`/api/projects/${pid}/dashboard`),
    enabled: Boolean(pid),
    refetchInterval: 8000,
  });
}

export function agentReady(agent: AgentRecord): boolean {
  return agent.enabled !== false && !agent.error && agent.command !== null;
}

export function formatLastActive(summary: ProjectSummary): string {
  return summary.lastActive ? fmtRelative(summary.lastActive) : "—";
}

