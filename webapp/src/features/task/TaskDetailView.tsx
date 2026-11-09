import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Bot,
  CheckCheck,
  Clock3,
  Copy,
  FileDiff,
  FileText,
  Flag,
  FolderOpen,
  GitCompare,
  GitMerge,
  Layers,
  LockKeyhole,
  MoreHorizontal,
  Pencil,
  Play,
  RefreshCw,
  RotateCcw,
  Shield,
  ShieldCheck,
  Square,
  Terminal,
  Timer,
  Trash2,
  Undo2,
  UploadCloud,
  UserRound,
  X,
} from "lucide-react";
import { ApiError, api, qk } from "../../api/client";
import type { MergeInfo, Task, TaskDetail } from "../../api/types";
import { useInvalidateProject, useTask } from "../../app/queries";
import { useUi } from "../../app/store";
import { StatusBadge } from "../../components/StatusBadge";
import { Badge, Button, Field, IconButton, Input, Panel } from "../../ui/primitives";
import { ConfirmDialog, Dialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import { KIND_LABEL, fmtRelative } from "../../lib/format";
import {
  asArray,
  copyText,
  openLocalPath,
  sortRuns,
  taskElapsed,
  taskGitInfo,
  taskIsolationInfo,
  taskMergeInfo,
  taskMergeState,
  useNow,
  type TaskTab,
} from "./helpers";
import { OverviewTab } from "./OverviewTab";
import { ChangesTab } from "./ChangesTab";
import { ValidationTab } from "./ValidationTab";
import { CompareTab } from "./CompareTab";
import { ActivityTab } from "./ActivityTab";
import { LogsTab } from "./LogsTab";
import { ContextTab } from "./ContextTab";
import { reviewKeys, useRebuildBranch } from "../review/review-api";
import "./task-detail.css";

type ActionKind =
  | "run"
  | "retry"
  | "cancel"
  | "approve"
  | "approveOnly"
  | "approveMerge"
  | "sync"
  | "merge"
  | "discardBranch"
  | "rebuildBranch"
  | "revert"
  | "skip"
  | "takeover"
  | "delete";

interface ConfirmState {
  kind: ActionKind;
  title: string;
  message: ReactNode;
  confirmLabel: string;
  danger?: boolean;
}

interface MergeResponse {
  merged?: boolean;
  conflict?: boolean;
  blocked?: boolean;
  reason?: string | null;
  branch?: string | null;
  base?: string | null;
  sha?: string | null;
  files?: string[];
  dirty_files?: string[];
  message?: string;
  error?: string | null;
  count?: number;
  committed?: string[];
}

interface MergeIssue {
  kind: "conflict" | "blocked" | "missing_branch" | "failed";
  missing_branch: boolean;
  conflict: boolean;
  blocked: boolean;
  reason?: string | null;
  branch?: string | null;
  base?: string | null;
  files: string[];
  dirty_files: string[];
  message?: string;
  error?: string | null;
}

interface RebuildBranchResponse {
  rebuilt?: boolean;
  branch?: string | null;
  base?: string | null;
  sha?: string | null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item ?? "")).filter((item) => item.length > 0)
    : [];
}

/** 把后端 409 顶层 JSON（merge 冲突 / 脏基线阻断）转成可渲染的面板状态。 */
function mergeIssueFromBody(value: unknown): MergeIssue | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const conflict = Boolean(record.conflict);
  const blocked = Boolean(record.blocked);
  const files = stringList(record.files);
  const dirtyFiles = stringList(record.dirty_files);
  const errorRecord =
    record.error && typeof record.error === "object"
      ? (record.error as Record<string, unknown>)
      : null;
  const detailRecord =
    errorRecord?.details && typeof errorRecord.details === "object"
      ? (errorRecord.details as Record<string, unknown>)
      : null;
  const reason =
    typeof record.reason === "string"
      ? record.reason
      : typeof record.code === "string"
        ? record.code
        : null;
  const code =
    typeof record.code === "string"
      ? record.code
      : typeof errorRecord?.code === "string"
        ? errorRecord.code
        : null;
  const missingBranch = reason === "missing_branch" || code === "missing_branch";
  if (!conflict && !blocked && !dirtyFiles.length && !missingBranch) return null;
  const kind: MergeIssue["kind"] = missingBranch
    ? "missing_branch"
    : conflict
      ? "conflict"
      : blocked
        ? "blocked"
        : "failed";
  return {
    kind,
    missing_branch: missingBranch,
    conflict,
    blocked,
    reason,
    branch:
      typeof record.branch === "string"
        ? record.branch
        : typeof detailRecord?.branch === "string"
          ? detailRecord.branch
          : null,
    base:
      typeof record.base === "string"
        ? record.base
        : typeof detailRecord?.base === "string"
          ? detailRecord.base
          : null,
    files,
    dirty_files: dirtyFiles,
    message:
      typeof record.message === "string"
        ? record.message
        : typeof errorRecord?.message === "string"
          ? errorRecord.message
          : undefined,
    error:
      typeof record.error === "string"
        ? record.error
        : typeof errorRecord?.message === "string"
          ? errorRecord.message
          : null,
  };
}

function isolationModeLabel(mode: string | null | undefined): string {
  switch (mode) {
    case "worktree":
      return "worktree";
    case "copy":
      return "copy";
    case "shared":
      return "共享";
    default:
      return mode || "隔离副本";
  }
}

/** 徽章文案里不重复「worktree 不可用」；完整原因仍放 title。 */
function isolationDegradeSummary(reason: string): string {
  return reason.replace(/[，,]\s*worktree\s*不可用\s*$/i, "").trim() || reason;
}

const ACTION_LABEL: Record<ActionKind, string> = {
  run: "启动运行",
  retry: "重新排队",
  cancel: "取消任务",
  approve: "验收并同步",
  approveOnly: "人工验收",
  approveMerge: "验收并合并",
  sync: "同步改动",
  merge: "合并到基线",
  discardBranch: "放弃任务分支",
  rebuildBranch: "重建分支",
  revert: "回滚改动",
  skip: "跳过任务",
  takeover: "人工接手",
  delete: "删除任务",
};

const ACTION_SUCCESS: Record<ActionKind, string> = {
  run: "任务已启动",
  retry: "任务已重新排队",
  cancel: "任务已取消",
  approve: "已验收并同步",
  approveOnly: "已人工验收",
  approveMerge: "已验收并合并",
  sync: "同步完成",
  merge: "已合并到基线",
  discardBranch: "任务分支已放弃",
  rebuildBranch: "任务分支已重建",
  revert: "改动已回滚",
  skip: "任务已跳过",
  takeover: "已转为人工接手",
  delete: "任务已删除",
};

const CONFIRM: Record<
  Exclude<ActionKind, "run" | "takeover">,
  Omit<ConfirmState, "kind">
> = {
  retry: {
    title: "重试这个任务？",
    message:
      "重试会启动新的 Agent 会话（不会复用上次 ACP 会话），并把上次 write_scope 内的文件恢复到新副本（文件级续跑）。",
    confirmLabel: "重试",
  },
  cancel: {
    title: "取消这次任务？",
    message: "正在运行的 Agent 会被终止；已经产生的改动与日志会保留，之后可以重试。",
    confirmLabel: "取消任务",
    danger: true,
  },
  approve: {
    title: "验收并同步？",
    message: "将把工作副本中的改动写入项目工作区，并把任务标记为成功。冲突、越界或受保护的文件会被跳过。",
    confirmLabel: "验收并同步",
  },
  approveOnly: {
    title: "只做人工验收？",
    message: "任务会被标记为人工验收通过，但不会把工作副本写入工作区。",
    confirmLabel: "仅验收",
  },
  approveMerge: {
    title: "验收并合并？",
    message:
      "先通过 git merge --no-ff 把任务分支合并到当前基线；成功后才把任务标记为人工验收通过（不做文件级同步）。合并失败会中止并保持任务为 waiting，转为「合并问题」等待人工处理。",
    confirmLabel: "验收并合并",
  },
  sync: {
    title: "同步到工作区？",
    message: "只同步 write_scope 内、未冲突且非受保护的路径。越界文件会被跳过。",
    confirmLabel: "同步",
  },
  merge: {
    title: "合并到基线？",
    message:
      "将通过 --no-ff 把任务分支合并到项目当前基线分支；合并前基线工作树必须干净。冲突不会自动解决，系统会中止合并并保持仓库干净。",
    confirmLabel: "合并到基线",
  },
  discardBranch: {
    title: "放弃任务分支？",
    message:
      "会删除任务工作副本，并强制删除任务分支；该分支上的任务提交会一并移除（当前基线分支受保护，不会被删除）。此操作不可撤销。",
    confirmLabel: "放弃任务分支",
    danger: true,
  },
  rebuildBranch: {
    title: "重建任务分支？",
    message:
      "将按任务记录的 commit/base 重新创建分支和工作副本。工作副本里未提交的内容可能丢失；重建后仍需重新检查改动并手动合并。",
    confirmLabel: "确认重建",
  },
  revert: {
    title: "回滚这次任务的改动？",
    message: "工作区里由该任务记录的文件改动会被撤销，任务回到待执行。此操作不可撤销。",
    confirmLabel: "回滚改动",
    danger: true,
  },
  skip: {
    title: "跳过这个任务？",
    message: "任务会被标记为已跳过，不会再被调度。依赖它的下游任务可能会因此阻塞。",
    confirmLabel: "跳过",
    danger: true,
  },
  delete: {
    title: "删除这个任务？",
    message: "任务记录会被删除，运行目录随后清理。此操作不可撤销，正在运行的任务不能删除。",
    confirmLabel: "删除任务",
    danger: true,
  },
};

function actionMessage(value: unknown): string | undefined {
  if (!value || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  for (const key of ["message", "detail", "reason"]) {
    if (typeof record[key] === "string" && record[key]) return String(record[key]);
  }
  return undefined;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function Popover({
  trigger,
  children,
  align = "end",
  width = 248,
}: {
  trigger: (props: { open: boolean; toggle: () => void }) => ReactNode;
  children: (props: { close: () => void }) => ReactNode;
  align?: "start" | "end";
  width?: number;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  return (
    <div className="td-menu-root" ref={rootRef}>
      {trigger({ open, toggle: () => setOpen((value) => !value) })}
      {open ? (
        <div
          className={clsx("td-menu", align === "end" && "is-end")}
          style={{ width }}
          role="menu"
        >
          {children({ close: () => setOpen(false) })}
        </div>
      ) : null}
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="page td-page">
      <div className="td-crumbs">
        <span className="skeleton td-skel-crumb" />
      </div>
      <div className="td-head">
        <div className="skeleton td-skel-title" />
        <div className="td-skel-meta">
          <span className="skeleton td-skel-badge" />
          <span className="skeleton td-skel-badge" />
          <span className="skeleton td-skel-badge" />
        </div>
      </div>
      <div className="td-skel-tabs">
        {[72, 64, 64, 64, 76].map((width, index) => (
          <span key={index} className="skeleton td-skel-tab" style={{ width }} />
        ))}
      </div>
      <div className="td-skel-body">
        <span className="skeleton td-skel-block" />
        <span className="skeleton td-skel-block" />
        <span className="skeleton td-skel-block" />
      </div>
    </div>
  );
}

export function TaskDetailView({
  taskId,
  onBack,
}: {
  taskId: string;
  onBack: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const taskTab = useUi((state) => state.taskTab);
  const setTaskTab = useUi((state) => state.setTaskTab);
  const fallbackProjectId = useUi((state) => state.projectId);
  const detailQuery = useTask(taskId);
  const detail = detailQuery.data;
  const task = detail?.task;
  const projectId = task?.project_id ?? fallbackProjectId ?? "";
  const invalidateProject = useInvalidateProject(projectId || null);
  const rebuildBranch = useRebuildBranch(taskId);
  const now = useNow(task?.status === "running");

  const [busy, setBusy] = useState<ActionKind | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleSaving, setTitleSaving] = useState(false);
  const [mergeIssue, setMergeIssue] = useState<MergeIssue | null>(null);
  const [timeoutOpen, setTimeoutOpen] = useState(false);
  const [timeoutValue, setTimeoutValue] = useState("1800");
  const [timeoutSaving, setTimeoutSaving] = useState(false);
  const titleInputRef = useRef<HTMLInputElement>(null);

  const runs = useMemo(() => sortRuns(asArray(detail?.runs)), [detail?.runs]);
  const changes = asArray(detail?.changes);
  const history = asArray(detail?.history);

  // 合并 UI 只由 taskMergeState 决定：git 任务绝不退化成「同步到工作区」。
  const mergeState = taskMergeState(task);
  const mergeInfo = taskMergeInfo(task);
  const gitInfo = taskGitInfo(task);
  const isolationInfo = taskIsolationInfo(task);
  const degradeReason = isolationInfo?.degrade_reason?.trim() || "";

  useEffect(() => {
    setSelectedRunId(null);
    setEditingTitle(false);
    setConfirm(null);
    setMergeIssue(null);
    setTimeoutOpen(false);
    setTimeoutSaving(false);
  }, [taskId]);

  useEffect(() => {
    if (!editingTitle) setTitleDraft(task?.title ?? "");
  }, [task?.title, editingTitle]);

  useEffect(() => {
    if (!editingTitle) return;
    requestAnimationFrame(() => {
      titleInputRef.current?.focus();
      titleInputRef.current?.select();
    });
  }, [editingTitle]);

  const openLogs = (runId?: string) => {
    setSelectedRunId(runId ?? null);
    setTaskTab("logs");
  };

  const invalidateTask = () => {
    void client.invalidateQueries({ queryKey: qk.task(taskId) });
    invalidateProject();
  };

  const execute = async (kind: ActionKind) => {
    if (!task) return;
    setBusy(kind);
    let approvedForMerge = false;
    try {
      let result: unknown;
      switch (kind) {
        case "run":
          result = await api.post(`/api/tasks/${task.id}/run`, { wait: 0 });
          break;
        case "retry":
          result = await api.post(`/api/tasks/${task.id}/retry`, {});
          break;
        case "cancel":
          result = await api.post(`/api/tasks/${task.id}/cancel`, {
            reason: "从任务详情页取消",
          });
          break;
        case "approve":
          result = await api.post(`/api/tasks/${task.id}/approve`, {
            sync: true,
            note: "人工验收通过（任务详情页）",
          });
          break;
        case "approveOnly":
          result = await api.post(`/api/tasks/${task.id}/approve`, {
            sync: false,
            note: "人工验收通过，暂不同步",
          });
          break;
        case "approveMerge":
          // waiting 的 git 任务：**先合并，后验收**。
          // 若先 approve 再 merge，merge 409 会把任务永久留在 success（下游会误判），
          // 因此合并失败时任务保持 waiting，用户可重试/放弃；只有 merge 成功后才
          // 用 approve {sync:false} 标记验收通过（不做文件级回收）。
          const mergeBeforeApprove = await api.post<MergeResponse>(
            `/api/tasks/${task.id}/merge`,
            {},
          );
          result = mergeBeforeApprove;
          if ((mergeBeforeApprove as MergeResponse).merged === false) {
            break;
          }
          try {
            await api.post(`/api/tasks/${task.id}/approve`, {
              sync: false,
              note: "任务分支已合并，人工验收通过（任务详情页）",
            });
          } catch (error) {
            toast.error(
              "合并已完成，但验收失败",
              `${errorMessage(error)}；任务仍为 waiting，可再次点击「验收并合并」重试验收。`,
            );
            invalidateTask();
            return;
          }
          approvedForMerge = true;
          break;
        case "sync":
          result = await api.post(`/api/tasks/${task.id}/sync`, {});
          break;
        case "merge":
          result = await api.post<MergeResponse>(`/api/tasks/${task.id}/merge`, {});
          break;
        case "discardBranch":
          result = await api.post(`/api/tasks/${task.id}/discard-worktree`, {
            delete_branch: true,
          });
          break;
        case "rebuildBranch":
          result = await rebuildBranch.mutateAsync();
          break;
        case "revert":
          result = await api.post(`/api/tasks/${task.id}/revert`, {});
          break;
        case "skip":
          result = await api.post(`/api/tasks/${task.id}/skip`, {
            reason: "人工跳过（任务详情页）",
          });
          break;
        case "takeover":
          result = await api.post(`/api/tasks/${task.id}/takeover`, { agent_id: "human" });
          break;
        case "delete":
          result = await api.del(`/api/tasks/${task.id}`);
          break;
      }

      if (kind === "delete") {
        toast.success(ACTION_SUCCESS[kind]);
        client.removeQueries({ queryKey: qk.task(task.id) });
        invalidateProject();
        onBack();
        return;
      }

      if (kind === "rebuildBranch") {
        const rebuilt = (result ?? {}) as RebuildBranchResponse;
        const branch = rebuilt.branch || mergeInfo?.branch || gitInfo?.branch || "";
        toast.success(
          ACTION_SUCCESS[kind],
          `${branch || "任务分支"}${rebuilt.sha ? ` @${rebuilt.sha.slice(0, 8)}` : ""}`,
        );
        setMergeIssue(null);
        invalidateTask();
        void client.invalidateQueries({ queryKey: reviewKeys.workspace(task.id) });
        void client.invalidateQueries({ queryKey: reviewKeys.changes(task.id) });
        void client.invalidateQueries({ queryKey: ["review", "diff", task.id] });
        void client.invalidateQueries({ queryKey: ["task-diff", task.id] });
        return;
      }

      if (kind === "merge" || kind === "approveMerge") {
        const merged = (result ?? {}) as MergeResponse;
        if (merged.merged === false) {
          // HTTP 200 但服务端明确没有合并：不能伪造成功，按合并问题展示。
          const issue = mergeIssueFromBody(merged);
          if (issue) setMergeIssue(issue);
          invalidateTask();
          toast.error(
            approvedForMerge ? "已验收，但未合并" : "合并未完成",
            merged.message || merged.error || merged.reason || "服务端未确认合并成功",
          );
          return;
        }
        const copyBack = merged.merged === undefined && Array.isArray(merged.committed);
        const branch = merged.branch || mergeInfo?.branch || gitInfo?.branch || "";
        const sha = String(merged.sha || mergeInfo?.sha || gitInfo?.commit || "").slice(0, 8);
        const fileCount = merged.files?.length ?? merged.count ?? 0;
        if (copyBack) {
          toast.success(
            approvedForMerge ? "已验收并同步到工作区" : "已同步到工作区",
            `${merged.committed?.length ?? 0} 个文件`,
          );
        } else {
          toast.success(
            approvedForMerge
              ? `已验收并合并到 ${branch || "基线"}${sha ? `（${sha}）` : ""}`
              : `已合并到 ${branch || "基线"}${sha ? `（${sha}）` : ""}`,
            fileCount ? `${fileCount} 个文件` : undefined,
          );
        }
        setMergeIssue(null);
        invalidateTask();
        void client.invalidateQueries({ queryKey: ["task-diff", task.id] });
        return;
      }

      if (kind === "discardBranch") {
        toast.success(ACTION_SUCCESS[kind], actionMessage(result));
        setMergeIssue(null);
        invalidateTask();
        return;
      }

      toast.success(ACTION_SUCCESS[kind], actionMessage(result));
      invalidateTask();
      if (kind === "run" || kind === "retry") {
        // 新一次尝试会覆盖上一次的合并现场，旧面板不应继续挂着。
        setMergeIssue(null);
        window.setTimeout(invalidateTask, 1200);
      }
    } catch (error) {
      if (
        (kind === "merge" || kind === "approveMerge") &&
        error instanceof ApiError &&
        error.status === 409
      ) {
        const issue = mergeIssueFromBody(error.body);
        if (issue) {
          // 409 必须转成页面内的「合并问题」面板，不能只弹一个 toast。
          setMergeIssue(issue);
          invalidateTask();
          const title = issue.conflict ? "合并冲突" : issue.blocked ? "合并被阻止" : "合并失败";
          toast.error(
            approvedForMerge ? `已验收，但${title}` : title,
            issue.message || issue.error || error.message,
          );
          return;
        }
      }
      if (kind === "approveMerge" && approvedForMerge) {
        // 验收已落库（任务已 success），但合并没有完成：必须 invalidate，
        // 让页面显示「未合并 / 合并问题」而不是假的已合并状态。
        invalidateTask();
        toast.error("验收已完成，但合并失败", errorMessage(error));
        return;
      }
      toast.error(`${ACTION_LABEL[kind]}失败`, errorMessage(error));
    } finally {
      setBusy(null);
    }
  };

  const request = (kind: ActionKind) => {
    if (!task) return;
    if (kind === "retry") {
      const blockedReason = String(task.blocked_reason || "");
      if (task.status === "blocked" && blockedReason.startsWith("merge_")) {
        // merge_* 阻塞必须走合并面板（重试合并 / 重建分支 / 放弃任务），
        // 普通 /retry 会被后端 409 拒绝：这里只提示，不发起请求。
        toast.info("请先处理合并冲突或重建分支", "合并类阻塞不能直接重试。");
        return;
      }
    }
    if (kind === "run" || kind === "takeover") {
      void execute(kind);
      return;
    }
    const config = CONFIRM[kind];
    if (kind === "merge") {
      const retrying =
        mergeState === "conflict" || mergeState === "dirty_blocked" || mergeState === "blocked";
      setConfirm({
        kind,
        ...config,
        title: retrying ? "重试合并？" : config.title,
        confirmLabel: retrying ? "重试合并" : config.confirmLabel,
      });
      return;
    }
    setConfirm({ kind, ...config });
  };

  const saveTitle = async () => {
    if (!task || titleSaving) return;
    const next = titleDraft.trim();
    if (!next) {
      toast.error("标题不能为空");
      setTitleDraft(task.title);
      setEditingTitle(false);
      return;
    }
    if (next === task.title) {
      setEditingTitle(false);
      return;
    }
    setTitleSaving(true);
    try {
      const updated = await api.patch<Task>(`/api/tasks/${task.id}`, { title: next });
      client.setQueryData<TaskDetail>(qk.task(task.id), (current) =>
        current
          ? {
              ...current,
              task: {
                ...current.task,
                ...(updated && typeof updated === "object" ? updated : {}),
                title: updated?.title || next,
              },
            }
          : current,
      );
      setEditingTitle(false);
      toast.success("标题已更新");
      invalidateTask();
    } catch (error) {
      toast.error("重命名失败", errorMessage(error));
      setTitleDraft(task.title);
    } finally {
      setTitleSaving(false);
    }
  };

  const openWorkdir = async () => {
    if (!task?.workdir) {
      toast.info("任务还没有工作目录", "执行一次后会自动生成隔离副本。");
      return;
    }
    const result = await openLocalPath(task.workdir);
    if (result === "desktop") toast.success("已打开工作目录");
    else if (result === "copied") toast.info("已复制工作目录路径", task.workdir);
    else toast.error("无法打开工作目录");
  };

  const openTimeoutDialog = () => {
    setTimeoutValue(String(task?.timeout_sec || 1800));
    setTimeoutOpen(true);
  };

  const saveTimeout = async () => {
    if (!task || timeoutSaving) return;
    const seconds = Math.floor(Number(timeoutValue));
    if (!Number.isFinite(seconds) || seconds < 30 || seconds > 86400) {
      toast.error("超时时间无效", "请输入 30–86400 之间的整数秒数。");
      return;
    }
    setTimeoutSaving(true);
    try {
      const response = await api.post<{ message?: string }>(`/api/tasks/${task.id}/timeout`, {
        timeout_sec: seconds,
      });
      toast.success("超时预算已更新", response?.message || `下一次尝试使用 ${seconds} 秒超时`);
      setTimeoutOpen(false);
      invalidateTask();
    } catch (error) {
      toast.error("更新超时失败", errorMessage(error));
    } finally {
      setTimeoutSaving(false);
    }
  };

  if (detailQuery.isLoading && !detail) return <DetailSkeleton />;

  if (detailQuery.isError || !task) {
    return (
      <div className="page td-page">
        <div className="td-crumbs">
          <Button size="sm" variant="ghost" onClick={onBack}>
            <ArrowLeft size={14} /> 返回任务
          </Button>
        </div>
        <Panel className="td-error-panel">
          <span className="td-error-icon">
            <AlertTriangle size={22} />
          </span>
          <h2>加载任务失败</h2>
          <p>{detailQuery.error ? errorMessage(detailQuery.error) : "任务不存在，或已被删除。"}</p>
          <div className="td-error-actions">
            <Button onClick={() => void detailQuery.refetch()}>
              <RefreshCw size={14} /> 重试
            </Button>
            <Button variant="ghost" onClick={onBack}>
              返回任务列表
            </Button>
          </div>
        </Panel>
      </div>
    );
  }

  const canSync =
    mergeState === null &&
    task.status === "success" &&
    !task.read_only &&
    task.isolation !== "shared" &&
    changes.length > 0;
  const blockedReason = String(task.blocked_reason || "");
  const mergeBlocked = task.status === "blocked" && blockedReason.startsWith("merge_");
  const canRetry = !mergeBlocked;
  const elapsed = taskElapsed(task, now);
  const isolationMode = isolationInfo?.mode || task.isolation;
  const isolationDegrade = degradeReason ? isolationDegradeSummary(degradeReason) : "";
  const isolationLabel = isolationInfo?.degraded
    ? `${isolationModeLabel(isolationMode)}（worktree 不可用${isolationDegrade ? `：${isolationDegrade}` : ""}）`
    : isolationModeLabel(isolationMode);
  const isolationTitle = isolationInfo?.degraded
    ? `${isolationInfo.degrade_reason || "worktree 不可用"}${
        isolationInfo.requested_mode ? `（请求模式：${isolationInfo.requested_mode}）` : ""
      }`
    : undefined;
  const mergedBranch = mergeInfo?.branch || gitInfo?.branch || "";
  const mergedShaFull = mergeInfo?.sha || gitInfo?.commit || "";
  const mergedShaShort = mergedShaFull ? mergedShaFull.slice(0, 8) : "";
  const nextAttempt =
    task.attempt > 0 ? `尝试 ${task.attempt}/${task.max_attempts || "—"}` : "尚未运行";

  let primaryAction: ReactNode;
  if (task.status === "running") {
    primaryAction = (
      <Button
        variant="danger"
        onClick={() => request("cancel")}
        loading={busy === "cancel"}
        disabled={Boolean(busy)}
      >
        <Square size={13} /> 取消
      </Button>
    );
  } else if (mergeState === "discarded") {
    // 分支/工作副本可能已不存在：绝不能留下必然 409 的合并/同步入口。
    primaryAction = (
      <span title="任务分支已放弃，未合并任何改动">
        <Badge tone="warning" className="td-merge-badge">
          <GitMerge size={12} /> 任务分支已放弃
        </Badge>
      </span>
    );
  } else if (mergeState === "missing_branch") {
    // 分支 ref 已被删除：主操作区不提供任何会直接调用 /merge 的入口。
    primaryAction = (
      <span title="任务分支已不存在，请先在合并面板中重建分支">
        <Badge tone="danger" className="td-merge-badge">
          <AlertTriangle size={12} /> 分支已不存在
        </Badge>
      </span>
    );
  } else if (task.status === "pending") {
    primaryAction = (
      <Button
        variant="primary"
        onClick={() => request("run")}
        loading={busy === "run"}
        disabled={Boolean(busy)}
      >
        <Play size={14} /> 运行
      </Button>
    );
  } else if (task.status === "waiting") {
    // waiting 的 git 任务必须「先 git merge，成功后再改状态为验收」；
    // 没有 git 交付元数据时保持文件级「验收并同步」。
    const waitingHasGit = Boolean(gitInfo) || mergeState !== null;
    primaryAction = waitingHasGit ? (
      <Button
        variant="primary"
        onClick={() => request("approveMerge")}
        loading={busy === "approveMerge"}
        disabled={Boolean(busy)}
        title="先 git merge --no-ff；成功后再标记验收通过（不做文件级同步）"
      >
        <GitMerge size={14} /> 验收并合并
      </Button>
    ) : (
      <Button
        variant="primary"
        onClick={() => request("approve")}
        loading={busy === "approve"}
        disabled={Boolean(busy)}
      >
        <CheckCheck size={14} /> 验收并同步
      </Button>
    );
  } else if (mergeState === "merged") {
    primaryAction = (
      <>
        <span title={mergedShaFull ? `合并提交 ${mergedShaFull}` : undefined}>
          <Badge tone="success" className="td-merge-badge">
            <GitMerge size={12} /> 已合并到{" "}
            <span className="mono">{mergedBranch || "基线"}</span>
            {mergedShaShort ? <span className="mono"> @{mergedShaShort}</span> : null}
          </Badge>
        </span>
        <Button
          variant="ghost"
          onClick={() => request("run")}
          loading={busy === "run"}
          disabled={Boolean(busy)}
        >
          <Play size={14} /> 再次运行
        </Button>
      </>
    );
  } else if (mergeState) {
    const retryMerge =
      mergeState === "conflict" || mergeState === "dirty_blocked" || mergeState === "blocked";
    primaryAction = (
      <Button
        variant="primary"
        onClick={() => request("merge")}
        loading={busy === "merge"}
        disabled={Boolean(busy)}
      >
        {retryMerge ? <RefreshCw size={14} /> : <GitMerge size={14} />}
        {retryMerge ? "重试合并" : "合并到基线"}
      </Button>
    );
  } else if (task.status === "failed" || task.status === "blocked" || task.status === "interrupted") {
    primaryAction = canRetry ? (
      <Button
        variant="primary"
        onClick={() => request("retry")}
        loading={busy === "retry"}
        disabled={Boolean(busy)}
      >
        <RotateCcw size={14} /> 重试
      </Button>
    ) : (
      <span title="merge_* 阻塞需先处理合并冲突或重建分支">
        <Badge tone="warning" className="td-merge-badge">
          <AlertTriangle size={12} /> 请先处理合并冲突或重建分支
        </Badge>
      </span>
    );
  } else if (canSync) {
    primaryAction = (
      <Button
        variant="primary"
        onClick={() => request("sync")}
        loading={busy === "sync"}
        disabled={Boolean(busy)}
      >
        <UploadCloud size={14} /> 同步到工作区
      </Button>
    );
  } else {
    primaryAction = (
      <Button
        variant="primary"
        onClick={() => request("run")}
        loading={busy === "run"}
        disabled={Boolean(busy)}
      >
        <Play size={14} /> 再次运行
      </Button>
    );
  }

  const mergeInfoCode = String(
    (mergeInfo as (MergeInfo & { code?: string | null }) | null)?.code ?? "",
  );
  const storedMergeIssue: MergeIssue | null =
    mergeState === "conflict" ||
    mergeState === "dirty_blocked" ||
    mergeState === "blocked" ||
    mergeState === "missing_branch"
      ? {
          kind:
            mergeState === "missing_branch"
              ? "missing_branch"
              : mergeState === "conflict"
                ? "conflict"
                : "blocked",
          missing_branch: mergeState === "missing_branch",
          conflict: mergeState === "conflict",
          blocked: mergeState !== "conflict" && mergeState !== "missing_branch",
          reason: mergeInfo?.reason ?? mergeInfoCode ?? null,
          branch: mergeInfo?.branch || gitInfo?.branch || null,
          base: mergeInfo?.base || gitInfo?.base || null,
          files: stringList(mergeInfo?.files),
          dirty_files: stringList(mergeInfo?.dirty_files),
          message:
            mergeInfo?.error ||
            (mergeState === "missing_branch"
              ? "任务分支已被删除，无法直接合并；可按记录的 commit/base 重建分支，或放弃任务。"
              : undefined),
          error: mergeInfo?.error ?? null,
        }
      : null;
  const mergeProblem = mergeState === "discarded" ? null : mergeIssue ?? storedMergeIssue;
  const mergeProblemMissing = Boolean(mergeProblem?.missing_branch);
  const mergeProblemIsConflict = Boolean(mergeProblem?.conflict);
  const mergeProblemDirty = Boolean(
    mergeProblem &&
      !mergeProblemMissing &&
      !mergeProblemIsConflict &&
      (mergeProblem.reason === "dirty_worktree" || mergeProblem.dirty_files.length > 0),
  );
  const mergeProblemTitle = mergeProblemMissing
    ? "分支已不存在，无法合并"
    : mergeProblem?.conflict
      ? "合并冲突"
      : mergeProblemDirty
        ? "基线工作树有未提交改动"
        : "合并被阻止";
  const mergeProblemFiles = mergeProblem
    ? mergeProblem.conflict
      ? mergeProblem.files
      : mergeProblem.dirty_files.length
        ? mergeProblem.dirty_files
        : mergeProblem.files
    : [];
  const mergeProblemShown = mergeProblemFiles.slice(0, 12);
  const mergeProblemBase = String(mergeProblem?.base || "").slice(0, 8);
  const mergeProblemMessage = mergeProblem?.message || mergeProblem?.error || "";

  const tabs: Array<{ key: TaskTab; label: string; icon: ReactNode; count?: number }> = [
    { key: "overview", label: "概览", icon: <FileText size={14} /> },
    { key: "changes", label: "改动", icon: <FileDiff size={14} />, count: changes.length },
    { key: "validation", label: "验证", icon: <ShieldCheck size={14} /> },
    {
      key: "compare",
      label: "对比",
      icon: <GitCompare size={14} />,
      count: runs.length > 1 ? runs.length : undefined,
    },
    {
      key: "activity",
      label: "活动",
      icon: <Activity size={14} />,
      count: history.length + runs.length,
    },
    { key: "logs", label: "日志", icon: <Terminal size={14} />, count: runs.length },
    { key: "context", label: "上下文", icon: <Layers size={14} /> },
  ];

  return (
    <div className="page td-page">
      <div className="td-crumbs">
        <Button size="sm" variant="ghost" onClick={onBack}>
          <ArrowLeft size={14} /> 任务
        </Button>
        <span className="td-crumb-sep">/</span>
        <span className="td-crumb-current">任务详情</span>
        <span className="td-crumb-id mono" title={task.id}>
          #{task.id.slice(0, 8)}
        </span>
        <span className="td-crumbs-spacer" />
        <IconButton label="刷新任务" onClick={() => void detailQuery.refetch()}>
          <RefreshCw size={14} className={detailQuery.isFetching ? "spin" : undefined} />
        </IconButton>
      </div>

      <header className="td-head">
        <div className="td-head-top">
          <div className="td-title-block">
            {editingTitle ? (
              <div className="td-title-edit">
                <input
                  ref={titleInputRef}
                  className="td-title-input"
                  value={titleDraft}
                  maxLength={240}
                  spellCheck={false}
                  aria-label="任务标题"
                  onChange={(event) => setTitleDraft(event.target.value)}
                  onBlur={() => void saveTitle()}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void saveTitle();
                    } else if (event.key === "Escape") {
                      event.preventDefault();
                      event.stopPropagation();
                      setTitleDraft(task.title);
                      setEditingTitle(false);
                    }
                  }}
                />
                <div className="td-title-edit-actions">
                  <IconButton
                    label="保存标题"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => void saveTitle()}
                    disabled={titleSaving}
                  >
                    <CheckCheck size={15} />
                  </IconButton>
                  <IconButton
                    label="取消编辑"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      setTitleDraft(task.title);
                      setEditingTitle(false);
                    }}
                  >
                    <X size={15} />
                  </IconButton>
                </div>
              </div>
            ) : (
              <div className="td-title-display">
                <h1
                  className="td-title"
                  title="双击重命名"
                  onDoubleClick={() => {
                    setTitleDraft(task.title);
                    setEditingTitle(true);
                  }}
                >
                  {task.title || "未命名任务"}
                </h1>
                <button
                  type="button"
                  className="td-title-pencil"
                  aria-label="重命名任务"
                  title="重命名"
                  onClick={() => {
                    setTitleDraft(task.title);
                    setEditingTitle(true);
                  }}
                >
                  <Pencil size={13} />
                </button>
              </div>
            )}
            {editingTitle ? <div className="td-title-hint">Enter 保存 · Esc 取消</div> : null}
          </div>

          <div className="td-head-actions">
            {runs.length ? (
              <IconButton label="查看日志" onClick={() => openLogs()}>
                <Terminal size={15} />
              </IconButton>
            ) : null}
            {primaryAction}
            <Popover
              align="end"
              width={248}
              trigger={({ open, toggle }) => (
                <Button
                  variant={open ? "default" : "ghost"}
                  aria-label="更多任务操作"
                  title="更多"
                  onClick={toggle}
                  onKeyDown={(event) => {
                    if (event.key === "Escape" && open) {
                      event.preventDefault();
                      event.stopPropagation();
                      toggle();
                    }
                  }}
                  className="td-more-button"
                >
                  <MoreHorizontal size={16} />
                </Button>
              )}
            >
              {({ close }) => (
                <div
                  className="menu"
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      event.preventDefault();
                      event.stopPropagation();
                      close();
                    }
                  }}
                >
                  <div className="menu-label">任务</div>
                  <button
                    type="button"
                    className="menu-item"
                    disabled={!task.workdir}
                    onClick={() => {
                      close();
                      void openWorkdir();
                    }}
                  >
                    <Shield size={14} />
                    <span className="menu-item-text">
                      打开工作目录
                      <span className="menu-item-sub">{task.workdir || "运行后生成"}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    className="menu-item"
                    onClick={async () => {
                      close();
                      const ok = await copyText(task.id);
                      if (ok) toast.success("已复制任务 ID");
                      else toast.error("复制失败");
                    }}
                  >
                    <Copy size={14} /> 复制任务 ID
                  </button>
                  <button
                    type="button"
                    className="menu-item"
                    onClick={async () => {
                      close();
                      const ok = await copyText(task.title);
                      if (ok) toast.success("已复制标题");
                      else toast.error("复制失败");
                    }}
                  >
                    <Copy size={14} /> 复制标题
                  </button>
                  <button
                    type="button"
                    className="menu-item"
                    onClick={() => {
                      close();
                      openTimeoutDialog();
                    }}
                  >
                    <Timer size={14} />
                    <span className="menu-item-text">
                      延长超时
                      <span className="menu-item-sub">
                        当前 {task.timeout_sec ? `${task.timeout_sec}s` : "继承策略"} · 下一次尝试生效
                      </span>
                    </span>
                  </button>

                  <div className="menu-separator" />
                  <div className="menu-label">处理</div>
                  {task.status === "waiting" ? (
                    <button
                      type="button"
                      className="menu-item"
                      onClick={() => {
                        close();
                        request("approveOnly");
                      }}
                    >
                      <CheckCheck size={14} />
                      <span className="menu-item-text">
                        仅验收，不同步
                        <span className="menu-item-sub">只改任务状态</span>
                      </span>
                    </button>
                  ) : null}
                  {mergeState === "discarded" ? (
                    <button
                      type="button"
                      className="menu-item"
                      onClick={() => {
                        close();
                        request("run");
                      }}
                    >
                      <Play size={14} /> 再次运行
                    </button>
                  ) : null}
                  {canRetry &&
                  (task.status === "failed" ||
                    task.status === "blocked" ||
                    task.status === "interrupted" ||
                    task.status === "cancelled" ||
                    task.status === "skipped") ? (
                    <button
                      type="button"
                      className="menu-item"
                      onClick={() => {
                        close();
                        request("retry");
                      }}
                    >
                      <RotateCcw size={14} /> 重新排队
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="menu-item"
                    disabled={
                      task.status === "running" ||
                      task.status === "success" ||
                      task.status === "cancelled" ||
                      task.status === "skipped"
                    }
                    onClick={() => {
                      close();
                      request("takeover");
                    }}
                  >
                    <UserRound size={14} /> 人工接手
                  </button>
                  <button
                    type="button"
                    className="menu-item"
                    disabled={task.status === "running" || !changes.length}
                    onClick={() => {
                      close();
                      request("revert");
                    }}
                  >
                    <Undo2 size={14} /> 回滚改动
                  </button>
                  <button
                    type="button"
                    className="menu-item"
                    disabled={
                      task.status === "running" ||
                      task.status === "success" ||
                      task.status === "skipped"
                    }
                    onClick={() => {
                      close();
                      request("skip");
                    }}
                  >
                    <X size={14} /> 跳过任务
                  </button>

                  <div className="menu-separator" />
                  <button
                    type="button"
                    className="menu-item is-danger"
                    disabled={task.status === "running"}
                    onClick={() => {
                      close();
                      request("delete");
                    }}
                  >
                    <Trash2 size={14} /> 删除任务
                  </button>
                </div>
              )}
            </Popover>
          </div>
        </div>

        <div className="td-meta">
          <StatusBadge status={task.status} />
          <span className="td-meta-item">
            <Bot size={12} /> {task.agent_id || "自动路由"}
          </span>
          <span className="td-meta-item">
            <Layers size={12} /> {KIND_LABEL[task.kind] ?? task.kind}
          </span>
          <span className="td-meta-item">
            <RotateCcw size={12} /> {nextAttempt}
          </span>
          <span className="td-meta-item">
            <Clock3 size={12} /> {elapsed}
          </span>
          <span className="td-meta-item">
            <Flag size={12} /> P{task.priority ?? 0}
          </span>
          <span className="td-isolation-badge-wrap" title={isolationTitle}>
            <Badge tone={isolationInfo?.degraded ? "warning" : "outline"}>
              {isolationInfo?.degraded ? <AlertTriangle size={11} /> : null}
              {isolationLabel}
            </Badge>
          </span>
          {task.read_only ? (
            <Badge tone="warning">
              <LockKeyhole size={11} /> 只读
            </Badge>
          ) : null}
          <span className="td-meta-updated">更新于 {fmtRelative(task.updated_at)}</span>
        </div>
      </header>

      {mergeProblem ? (
        <section className="td-merge-issue" aria-label="合并问题">
          <div className="td-merge-issue-head">
            <span className="td-merge-issue-icon">
              <GitMerge size={16} />
            </span>
            <div className="td-merge-issue-main">
              <div className="td-merge-issue-title">{mergeProblemTitle}</div>
              <div className="td-merge-issue-meta">
                <span>
                  任务分支 <code className="mono">{mergeProblem.branch || "—"}</code>
                </span>
                <span>
                  基线 <code className="mono">{mergeProblemBase || "—"}</code>
                </span>
                {mergeProblem.reason ? (
                  <span>
                    原因 <code className="mono">{mergeProblem.reason}</code>
                  </span>
                ) : null}
              </div>
            </div>
          </div>
          {mergeProblemMessage ? (
            <div className="td-merge-issue-message">{mergeProblemMessage}</div>
          ) : null}
          {mergeProblemFiles.length ? (
            <div className="td-merge-issue-files">
              <div className="td-merge-issue-files-label">
                {mergeProblemIsConflict
                  ? `冲突文件（${mergeProblemFiles.length}）`
                  : `未提交文件（${mergeProblemFiles.length}）`}
              </div>
              <ul className="td-merge-issue-file-list">
                {mergeProblemShown.map((path, index) => (
                  <li key={`${path}-${index}`}>{path}</li>
                ))}
              </ul>
              {mergeProblemFiles.length > mergeProblemShown.length ? (
                <div className="td-merge-issue-more">…等 {mergeProblemFiles.length} 个</div>
              ) : null}
            </div>
          ) : null}
          <div className="td-merge-issue-hint">
            {mergeProblemMissing
              ? "任务分支已被删除，不能直接重试合并。重建会按任务记录的 commit/base 恢复分支；重建前请确认工作副本里没有需要保留的未提交内容。"
              : "系统不会自动解决冲突；请在工作目录手动解决后重试合并。"}
            {mergeProblemDirty
              ? " 基线工作树有未提交改动时，请先提交或清理基线，再重试合并。"
              : ""}
          </div>
          <div className="td-merge-issue-actions">
            {mergeProblemMissing ? (
              <>
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => request("rebuildBranch")}
                  loading={busy === "rebuildBranch"}
                  disabled={Boolean(busy)}
                >
                  <RefreshCw size={13} /> 重建分支
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => request("discardBranch")}
                  disabled={Boolean(busy)}
                >
                  <Trash2 size={13} /> 放弃任务
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void openWorkdir()}
                  disabled={Boolean(busy)}
                >
                  <FolderOpen size={13} /> 打开工作目录
                </Button>
              </>
            ) : (
              <>
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => void execute("merge")}
                  loading={busy === "merge"}
                  disabled={Boolean(busy)}
                >
                  <RefreshCw size={13} /> 重试合并
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => request("discardBranch")}
                  disabled={Boolean(busy)}
                >
                  <Trash2 size={13} /> 放弃任务分支
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void openWorkdir()}
                  disabled={Boolean(busy)}
                >
                  <FolderOpen size={13} /> 打开工作目录
                </Button>
              </>
            )}
          </div>
        </section>
      ) : null}

      <nav className="td-tabs" role="tablist" aria-label="任务详情">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={taskTab === tab.key}
            className={clsx("td-tab", taskTab === tab.key && "is-active")}
            onClick={() => setTaskTab(tab.key)}
          >
            {tab.icon}
            <span>{tab.label}</span>
            {tab.count !== undefined ? <span className="td-tab-count">{tab.count}</span> : null}
          </button>
        ))}
      </nav>

      <div className="td-body">
        {taskTab === "overview" ? (
          <OverviewTab
            detail={detail as TaskDetail}
            onTab={setTaskTab}
            onOpenLogs={openLogs}
            onRetry={() => request("retry")}
            retryPending={busy === "retry"}
          />
        ) : null}
        {taskTab === "changes" ? (
          <ChangesTab
            detail={detail as TaskDetail}
            taskId={task.id}
            task={task}
            onSynced={invalidateTask}
          />
        ) : null}
        {taskTab === "validation" ? (
          <ValidationTab taskId={task.id} projectId={projectId} />
        ) : null}
        {taskTab === "compare" ? (
          <CompareTab taskId={task.id} task={task} runs={runs} onChanged={invalidateTask} />
        ) : null}
        {taskTab === "activity" ? (
          <ActivityTab
            detail={detail as TaskDetail}
            taskId={task.id}
            projectId={projectId}
            onOpenLogs={openLogs}
          />
        ) : null}
        {taskTab === "logs" ? (
          <LogsTab
            taskId={task.id}
            runs={runs}
            selectedRunId={selectedRunId}
            onSelectRun={(runId) => setSelectedRunId(runId)}
          />
        ) : null}
        {taskTab === "context" ? <ContextTab detail={detail as TaskDetail} /> : null}
      </div>

      <ConfirmDialog
        open={Boolean(confirm)}
        title={confirm?.title ?? ""}
        message={confirm?.message ?? ""}
        confirmLabel={confirm?.confirmLabel ?? "确认"}
        danger={confirm?.danger}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          const pending = confirm;
          setConfirm(null);
          if (pending) void execute(pending.kind);
        }}
      />

      <Dialog
        open={timeoutOpen}
        onClose={() => {
          if (!timeoutSaving) setTimeoutOpen(false);
        }}
        title="延长超时"
        description="调整任务的下一次尝试超时预算。"
        width={420}
        footer={
          <>
            <Button onClick={() => setTimeoutOpen(false)} disabled={timeoutSaving}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={timeoutSaving}
              onClick={() => void saveTimeout()}
            >
              保存
            </Button>
          </>
        }
      >
        <Field
          label="超时预算（秒）"
          hint="范围 30–86400 秒。对下一次尝试生效，不会延长正在运行的上次调用。"
        >
          <Input
            type="number"
            min={30}
            max={86400}
            step={30}
            value={timeoutValue}
            autoFocus
            onChange={(event) => setTimeoutValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void saveTimeout();
              }
            }}
          />
        </Field>
        {task.runtime?.next_attempt_timeout_sec ? (
          <div className="td-timeout-hint">
            下一次尝试的计划超时：{task.runtime.next_attempt_timeout_sec}s
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}
