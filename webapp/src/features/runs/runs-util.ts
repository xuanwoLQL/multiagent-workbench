import { useEffect, useState } from "react";
import type { RunRecord } from "./runs-api";

export type RunSortKey = "started" | "duration";

export interface RunSort {
  key: RunSortKey;
  dir: "asc" | "desc";
}

export interface RunSummary {
  total: number;
  finished: number;
  success: number;
  running: number;
  successRate: number;
  durationMs: number;
  tokens: number;
  cost: number;
}

const startedMs = (run: RunRecord): number => {
  const value = run.started_at ? new Date(run.started_at).getTime() : NaN;
  return Number.isNaN(value) ? 0 : value;
};

export function runDurationMs(run: RunRecord): number {
  const value = Number(run.duration_ms);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export function sortRuns(runs: RunRecord[], sort: RunSort): RunRecord[] {
  const factor = sort.dir === "asc" ? 1 : -1;
  return [...runs].sort((a, b) => {
    const left = sort.key === "started" ? startedMs(a) : runDurationMs(a);
    const right = sort.key === "started" ? startedMs(b) : runDurationMs(b);
    if (left === right) return startedMs(b) - startedMs(a);
    return (left - right) * factor;
  });
}

export function summarizeRuns(runs: RunRecord[]): RunSummary {
  let durationMs = 0;
  let tokens = 0;
  let cost = 0;
  let success = 0;
  let finished = 0;
  let running = 0;

  for (const run of runs) {
    durationMs += runDurationMs(run);
    tokens += Number(run.cost_tokens) || 0;
    cost += Number(run.cost_amount) || 0;
    if (run.status === "running") {
      running += 1;
      continue;
    }
    if (run.status === "success") success += 1;
    if (["success", "failed", "cancelled", "interrupted", "blocked"].includes(run.status)) {
      finished += 1;
    }
  }

  return {
    total: runs.length,
    finished,
    success,
    running,
    successRate: finished > 0 ? success / finished : 0,
    durationMs,
    tokens,
    cost,
  };
}

export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value === 0) return "¥0.00";
  if (value < 0.01) return `¥${value.toFixed(4)}`;
  return `¥${value.toFixed(2)}`;
}

export function formatPercent(rate: number): string {
  if (!Number.isFinite(rate) || rate <= 0) return rate === 0 ? "0%" : "—";
  const percent = rate * 100;
  return `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
}

export function formatChars(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

export function shortId(id: string | null | undefined, length = 8): string {
  if (!id) return "—";
  return id.length > length ? id.slice(0, length) : id;
}

export function runTitle(run: RunRecord, titles?: Map<string, string>): string {
  const fromTasks = titles?.get(run.task_id);
  const title = (fromTasks || run.task_title || "").trim();
  return title || `任务 ${shortId(run.task_id)}`;
}

/** Ticking clock used for live duration labels; only runs an interval while active. */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

export function downloadText(filename: string, text: string): void {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
