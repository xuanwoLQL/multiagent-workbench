import { useEffect, useState } from "react";
import type {
  MergeInfo,
  Run,
  Task,
  TaskCostInfo,
  TaskGitInfo,
  TaskIsolationInfo,
  TaskProcessInfo,
} from "../../api/types";
import { STATUS_LABEL, fmtDuration } from "../../lib/format";

export type TaskTab =
  | "overview"
  | "changes"
  | "validation"
  | "compare"
  | "activity"
  | "logs"
  | "context";

interface DesktopBridge {
  openPath?: (target: string) => Promise<unknown> | unknown;
}

export function desktopBridge(): DesktopBridge | null {
  const candidate = (window as unknown as { multiagentDesktop?: DesktopBridge })
    .multiagentDesktop;
  return candidate && typeof candidate === "object" ? candidate : null;
}

export async function copyText(text: string): Promise<boolean> {
  const value = String(text ?? "");
  if (!value) return false;
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    /* fall through to the legacy path (HTTP pages / denied permission) */
  }
  try {
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

export async function openLocalPath(target: string): Promise<"desktop" | "copied" | "failed"> {
  const path = String(target ?? "").trim();
  if (!path) return "failed";
  const bridge = desktopBridge();
  if (bridge?.openPath) {
    try {
      const result = await bridge.openPath(path);
      if (typeof result === "string" && result.trim()) {
        throw new Error(result);
      }
      return "desktop";
    } catch {
      /* fall back to copying the path below */
    }
  }
  return (await copyText(path)) ? "copied" : "failed";
}

export function downloadText(filename: string, text: string, mime = "text/plain;charset=utf-8") {
  const blob = new Blob([text ?? ""], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "download.txt";
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.setTimeout(() => URL.revokeObjectURL(url), 1200);
}

export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

export function taskElapsed(task: Task, now: number): string {
  const start = task.started_at ? new Date(task.started_at).getTime() : NaN;
  if (Number.isNaN(start)) return "—";
  const finished = task.finished_at ? new Date(task.finished_at).getTime() : NaN;
  const end = Number.isNaN(finished) ? (task.status === "running" ? now : NaN) : finished;
  if (Number.isNaN(end) || end < start) return "—";
  return fmtDuration(end - start);
}

export function runDuration(run: Run): number {
  if (run.duration_ms > 0) return run.duration_ms;
  if (!run.started_at) return 0;
  const start = new Date(run.started_at).getTime();
  const end = run.finished_at ? new Date(run.finished_at).getTime() : NaN;
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return 0;
  return end - start;
}

export function runTitle(run: Run): string {
  const agent = run.agent_label || run.agent_id || "agent";
  return `${agent} · 第 ${run.attempt || 1} 次尝试`;
}

export function sortRuns(runs: Run[] | undefined): Run[] {
  return [...(runs ?? [])].sort((left, right) => {
    const a = left.started_at ? new Date(left.started_at).getTime() : 0;
    const b = right.started_at ? new Date(right.started_at).getTime() : 0;
    return b - a;
  });
}

export function statusLabel(status: string): string {
  return STATUS_LABEL[status as keyof typeof STATUS_LABEL] ?? status;
}

export interface HistoryEntry {
  status: string;
  from?: string;
  at: string;
  reason?: string;
}

export function normalizeHistory(raw: unknown): HistoryEntry[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => {
    const record = item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    const status = String(
      record.to_status ?? record.status ?? record.to ?? record.kind ?? record.state ?? "unknown",
    );
    const fromRaw = record.from_status ?? record.from;
    return {
      status,
      from: fromRaw === undefined || fromRaw === null ? undefined : String(fromRaw),
      at: String(record.at ?? record.created_at ?? ""),
      reason:
        record.reason === undefined || record.reason === null ? undefined : String(record.reason),
    };
  });
}

/**
 * Agent handoff text sometimes arrives as one token per line (stream fragments).
 * Collapse single newlines into spaces while keeping blank-line paragraphs and
 * genuine bullet lists intact.
 */
export function normalizeMultilineText(raw: unknown): string {
  const text = String(raw ?? "");
  if (!text.includes("\n")) return text;
  const lines = text.split(/\r?\n/);
  const bulletLines = lines.filter((line) => /^\s*([-*•]|\d+[.)])\s+/.test(line)).length;
  if (bulletLines >= 2 && bulletLines >= lines.length * 0.35) return text;
  return text
    .split(/\n{2,}/)
    .map((paragraph) => joinFragments(paragraph.split(/\n/)))
    .filter(Boolean)
    .join("\n\n");
}

const STANDALONE_WORDS = new Set([
  "a",
  "an",
  "as",
  "at",
  "be",
  "by",
  "do",
  "he",
  "if",
  "in",
  "is",
  "it",
  "my",
  "no",
  "of",
  "on",
  "or",
  "so",
  "to",
  "up",
  "us",
  "we",
]);

function joinFragments(rawLines: string[]): string {
  const tokens = rawLines.map((line) => line.trim()).filter(Boolean);
  let output = "";
  for (const token of tokens) {
    if (!output) {
      output = token;
      continue;
    }
    const previousChar = output.slice(-1);
    const previousToken = output.split(/\s+/).pop() || "";
    const cjk =
      /[\u3400-\u9fff]$/.test(previousChar) && /^[\u3400-\u9fff]/.test(token);
    const noSpace =
      cjk ||
      /^[.,;:!?)\]}"'’%\-]/.test(token) ||
      /[(\[{/"'‘’\-]$/.test(previousChar) ||
      (previousToken.length === 1 &&
        /[A-Z0-9]/.test(previousToken) &&
        /^[a-z]/.test(token)) ||
      (previousToken.length === 1 && /[0-9]/.test(previousToken) && /^[A-Z]$/.test(token)) ||
      (/^[A-Z]{2,3}$/.test(previousToken) && /^[A-Z]$/.test(token)) ||
      (/^[a-z]+-[a-z]$/.test(previousToken) && /^[a-z]/.test(token)) ||
      (/^[a-z]{1,2}$/.test(token) &&
        /[a-z]{3,}$/.test(previousToken) &&
        !STANDALONE_WORDS.has(token));
    output += (noSpace ? "" : " ") + token;
  }
  return output;
}

export function asArray<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

export function totalTokens(runs: Run[]): number {
  return runs.reduce((sum, run) => sum + (Number(run.cost_tokens) || 0), 0);
}

export function totalRuntime(runs: Run[]): number {
  return runs.reduce((sum, run) => sum + runDuration(run), 0);
}

export function safeJson(value: unknown): string {
  if (value === null || value === undefined) return "{}";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const ACCEPTANCE_LABEL: Record<string, string> = {
  exit_zero: "退出码为 0",
  file_modified: "工作区有文件改动",
  outputs_exist: "产出文件存在",
  outputs_nonempty: "产出文件非空",
  verify_command: "验证命令通过",
  handoff_present: "生成交接说明",
  manual: "人工确认",
  timeout: "在超时预算内完成",
};

const DETAIL_LABEL: Record<string, string> = {
  paths: "路径",
  files: "文件",
  command: "命令",
  min_bytes: "最小字节",
  exit_code: "退出码",
  expect: "期望",
  pattern: "匹配",
  reason: "原因",
};

function renderDetailValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ");
  if (value && typeof value === "object") return safeJson(value);
  return String(value);
}

export interface AcceptanceItem {
  label: string;
  details: Array<{ label: string; value: string }>;
}

export function describeAcceptance(raw: unknown): AcceptanceItem {
  if (typeof raw === "string") {
    return { label: ACCEPTANCE_LABEL[raw] ?? raw, details: [] };
  }
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const record = raw as Record<string, unknown>;
    const type = String(record.type ?? record.check ?? record.kind ?? "condition");
    const details: Array<{ label: string; value: string }> = [];
    for (const [key, value] of Object.entries(record)) {
      if (key === "type" || key === "check" || key === "kind" || value === undefined || value === null) {
        continue;
      }
      if (value === "" || (Array.isArray(value) && value.length === 0)) continue;
      details.push({ label: DETAIL_LABEL[key] ?? key, value: renderDetailValue(value) });
    }
    return { label: ACCEPTANCE_LABEL[type] ?? type, details };
  }
  if (Array.isArray(raw)) {
    return { label: raw.map((item) => String(item)).join(" + "), details: [] };
  }
  return { label: String(raw ?? "—"), details: [] };
}

export function taskGitInfo(task: Task | undefined | null): TaskGitInfo | null {
  const value = task?.result?.git;
  return value && typeof value === "object" ? (value as TaskGitInfo) : null;
}

export function taskMergeInfo(task: Task | undefined | null): MergeInfo | null {
  const merge = taskGitInfo(task)?.merge;
  return merge && typeof merge === "object" ? (merge as MergeInfo) : null;
}

export type MergeState =
  | "merged"
  | "conflict"
  | "dirty_blocked"
  | "blocked"
  | "discarded"
  | "missing_branch"
  | "auto_merge_disabled"
  | "not_merged"
  | "pending";

/**
 * Derive one honest merge state for the UI. Never guess: when a task has no
 * git metadata at all (copy/shared isolation, non-git project) this returns
 * ``null`` so callers keep the file-level "sync" semantics.
 */
export function taskMergeState(task: Task | undefined | null): MergeState | null {
  const git = taskGitInfo(task);
  if (!git) return null;
  const merge = taskMergeInfo(task);
  if (!merge) return "pending";
  if (merge.merged) return "merged";
  if (merge.conflict) return "conflict";
  if (merge.discarded) return "discarded";
  const code = (merge as MergeInfo & { code?: string | null }).code;
  if (merge.reason === "missing_branch" || code === "missing_branch") return "missing_branch";
  if (merge.blocked) return merge.reason === "dirty_worktree" ? "dirty_blocked" : "blocked";
  if (merge.skipped) return "auto_merge_disabled";
  if (merge.reason === "dirty_worktree") return "dirty_blocked";
  return "not_merged";
}

export function taskCostInfo(task: Task | undefined | null): TaskCostInfo | null {
  const value = task?.result?.cost;
  return value && typeof value === "object" ? (value as TaskCostInfo) : null;
}

export function taskIsolationInfo(task: Task | undefined | null): TaskIsolationInfo | null {
  const value = task?.result?.isolation;
  return value && typeof value === "object" ? (value as TaskIsolationInfo) : null;
}

export function taskProcessInfo(task: Task | undefined | null): TaskProcessInfo | null {
  const value = task?.result?.processes;
  return value && typeof value === "object" ? (value as TaskProcessInfo) : null;
}

export function artifactExtension(path: string | undefined): string {
  const clean = String(path ?? "").toLowerCase();
  const index = clean.lastIndexOf(".");
  return index >= 0 ? clean.slice(index + 1) : "";
}

export function isImagePath(path: string | undefined): boolean {
  return ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"].includes(
    artifactExtension(path),
  );
}

export function isBinaryPath(path: string | undefined): boolean {
  return [
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "bmp",
    "ico",
    "zip",
    "gz",
    "7z",
    "jar",
    "class",
    "exe",
    "dll",
    "pdf",
    "mp4",
    "mp3",
    "wav",
  ].includes(artifactExtension(path));
}

export function fileName(path: string | undefined): string {
  const text = String(path ?? "");
  const parts = text.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || text || "产物";
}

export function dirName(path: string | undefined): string {
  const text = String(path ?? "").replace(/\\/g, "/");
  const index = text.lastIndexOf("/");
  return index > 0 ? text.slice(0, index) : "";
}

export function changeSymbol(action: string | undefined): { symbol: string; label: string } {
  switch (String(action ?? "").toLowerCase()) {
    case "create":
    case "add":
    case "added":
      return { symbol: "+", label: "新增" };
    case "delete":
    case "remove":
    case "deleted":
      return { symbol: "−", label: "删除" };
    case "rename":
    case "move":
      return { symbol: "→", label: "重命名" };
    default:
      return { symbol: "~", label: "修改" };
  }
}
