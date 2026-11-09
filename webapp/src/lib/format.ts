import type { TaskStatus } from "../api/types";

export const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: "待执行",
  running: "执行中",
  success: "成功",
  failed: "失败",
  blocked: "阻塞",
  waiting: "待人工",
  skipped: "已跳过",
  cancelled: "已取消",
  interrupted: "已中断",
};

export const STATUS_TONE: Record<
  TaskStatus,
  "default" | "success" | "warning" | "danger" | "info" | "accent"
> = {
  pending: "default",
  running: "accent",
  success: "success",
  failed: "danger",
  blocked: "warning",
  waiting: "warning",
  skipped: "default",
  cancelled: "default",
  interrupted: "danger",
};

export const KIND_LABEL: Record<string, string> = {
  generic: "通用",
  code: "代码",
  doc: "文档",
  data: "数据",
  research: "调研",
  review: "审阅",
  ppt: "演示",
  diagrams: "图表",
  web: "Web",
};

export function fmtInt(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function fmtBytes(value: number | null | undefined): string {
  if (!value || value < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let index = 0;
  while (n >= 1024 && index < units.length - 1) {
    n /= 1024;
    index += 1;
  }
  return `${n >= 100 || index === 0 ? Math.round(n) : n.toFixed(1)} ${units[index]}`;
}

export function fmtDuration(ms: number | null | undefined): string {
  if (!ms || ms < 0) return "—";
  const total = Math.round(ms / 1000);
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${(minutes % 60).toString().padStart(2, "0")}m`;
}

export function fmtClock(iso?: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDateTime(iso?: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function fmtRelative(iso?: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso).getTime();
  if (Number.isNaN(date)) return "—";
  const delta = Date.now() - date;
  if (delta < 45_000) return "刚刚";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
  if (delta < 7 * 86_400_000) return `${Math.floor(delta / 86_400_000)} 天前`;
  return fmtDateTime(iso);
}

export function initials(label: string): string {
  const clean = (label || "?").trim();
  const parts = clean.split(/[\s·/_-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return clean.slice(0, 2).toUpperCase();
}

export function agentKey(agent: { agent_id?: string; id?: string }): string {
  return agent.agent_id || agent.id || "agent";
}

const AGENT_COLORS = [
  "#3b5bdb",
  "#0d7a63",
  "#a1551b",
  "#7b3fb5",
  "#b42318",
  "#0d6b8a",
  "#4d5a1f",
];

export function agentColor(id: string): string {
  let hash = 0;
  for (let i = 0; i < id.length; i += 1) hash = (hash * 31 + id.charCodeAt(i)) | 0;
  return AGENT_COLORS[Math.abs(hash) % AGENT_COLORS.length];
}

export function shortPath(path: string, keep = 3): string {
  if (!path) return "";
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= keep + 1) return path;
  return `…/${parts.slice(-keep).join("/")}`;
}

export function stripAnsi(value: string): string {
  // eslint-disable-next-line no-control-regex
  return value.replace(/\u001b\[[0-9;]*[A-Za-z]/g, "");
}

export function relativeScope(scope: string[] | undefined): string {
  if (!scope?.length) return "整工作区";
  return scope.join(", ");
}
