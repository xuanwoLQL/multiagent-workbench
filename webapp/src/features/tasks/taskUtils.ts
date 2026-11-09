import type { Task, TaskStatus } from "../../api/types";
import { fmtDuration } from "../../lib/format";

export type ViewMode = "board" | "list";
export type SortMode = "updated" | "priority";

export interface BoardColumn {
  key: string;
  label: string;
  tone: "pending" | "running" | "waiting" | "success" | "danger";
  statuses: TaskStatus[];
  empty: string;
}

export const BOARD_COLUMNS: BoardColumn[] = [
  {
    key: "todo",
    label: "待执行",
    tone: "pending",
    statuses: ["pending", "interrupted"],
    empty: "队列是空的",
  },
  {
    key: "running",
    label: "执行中",
    tone: "running",
    statuses: ["running"],
    empty: "没有任务在运行",
  },
  {
    key: "waiting",
    label: "待人工",
    tone: "waiting",
    statuses: ["waiting"],
    empty: "没有等待人工确认的任务",
  },
  {
    key: "success",
    label: "成功",
    tone: "success",
    statuses: ["success"],
    empty: "还没有完成的任务",
  },
  {
    key: "failed",
    label: "失败 · 阻塞",
    tone: "danger",
    statuses: ["failed", "blocked", "cancelled", "skipped"],
    empty: "没有失败或阻塞的任务",
  },
];

const COLUMN_BY_STATUS = new Map<TaskStatus, string>();
for (const column of BOARD_COLUMNS) {
  for (const status of column.statuses) COLUMN_BY_STATUS.set(status, column.key);
}

export function columnOf(status: TaskStatus): string {
  return COLUMN_BY_STATUS.get(status) ?? "todo";
}

export function groupByColumn(tasks: Task[]): Record<string, Task[]> {
  const groups: Record<string, Task[]> = {};
  for (const column of BOARD_COLUMNS) groups[column.key] = [];
  for (const task of tasks) {
    const key = columnOf(task.status);
    (groups[key] ??= []).push(task);
  }
  return groups;
}

export function sortTasks(tasks: Task[], mode: SortMode): Task[] {
  const copy = [...tasks];
  copy.sort((a, b) => {
    if (mode === "priority") {
      const delta = (b.priority ?? 0) - (a.priority ?? 0);
      if (delta !== 0) return delta;
    }
    return timeOf(b.updated_at) - timeOf(a.updated_at);
  });
  return copy;
}

function timeOf(value: string | null | undefined): number {
  if (!value) return 0;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? 0 : time;
}

export function elapsedOf(task: Task, now: number): string {
  if (task.status === "running" && task.runtime?.elapsed_sec != null) {
    return fmtDuration(Number(task.runtime.elapsed_sec) * 1000);
  }
  if (!task.started_at) return "—";
  if (!task.finished_at && task.status !== "running") return "—";
  const start = timeOf(task.started_at);
  if (!start) return "—";
  const end = task.finished_at
    ? timeOf(task.finished_at)
    : task.status === "running"
      ? now
      : Date.now();
  if (!end || end < start) return "—";
  return fmtDuration(end - start);
}

export function priorityTone(priority: number | null | undefined): "high" | "mid" | "low" {
  if ((priority ?? 0) >= 2) return "high";
  if ((priority ?? 0) === 1) return "mid";
  return "low";
}

export function isRunnable(task: Task): boolean {
  return task.status === "pending" || task.status === "interrupted";
}

export function isRetryable(task: Task): boolean {
  return task.status === "failed" || task.status === "blocked" || task.status === "interrupted";
}

export function isActive(task: Task): boolean {
  return task.status === "running";
}

export function attemptLabel(task: Task): string {
  const max = Math.max(task.max_attempts ?? 1, 1);
  return `${task.runtime?.attempt ?? task.attempt ?? 0}/${task.runtime?.max_attempts ?? max}`;
}
