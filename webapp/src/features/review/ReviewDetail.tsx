import { useMemo, useState } from "react";
import { useQueries, useQueryClient, useIsFetching } from "@tanstack/react-query";
import { CheckCheck, ExternalLink, GitMerge, RefreshCw, Trash2, TriangleAlert } from "lucide-react";
import clsx from "clsx";
import { ApiError } from "../../api/client";
import type { ChangeRow, MergeInfo } from "../../api/types";
import { fmtBytes, fmtRelative } from "../../lib/format";
import { Badge, Button, EmptyState, IconButton, Spinner } from "../../ui/primitives";
import { ConfirmDialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import { StatusBadge } from "../../components/StatusBadge";
import { taskGitInfo, taskMergeState, type MergeState } from "../task/helpers";
import { DiffViewer, parseUnifiedDiff } from "./DiffViewer";
import {
  actionMeta,
  changeSize,
  reviewKeys,
  splitPath,
  taskDiffQuery,
  useDiscardBranch,
  useMergeTask,
  useRebuildBranch,
  useSyncTask,
  useTaskDiff,
  type ReviewEntry,
} from "./review-api";

const MAX_SUMMARY_FILES = 80;
const MAX_ISSUE_FILES = 12;

type MergeIssueKind = "conflict" | "blocked" | "missing_branch" | "failed";

interface MergeIssue {
  kind: MergeIssueKind;
  branch: string | null;
  base: string | null;
  files: string[];
  dirtyFiles: string[];
  message: string;
  reason: string | null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

/**
 * 409 响应体是顶层 JSON（不是 FastAPI 的 detail 包裹）；``api.post`` 会把它放进
 * ``ApiError.body``。这里只读取展示字段，绝不尝试自动解决冲突。
 */
function parseMergeIssue(
  body: unknown,
  fallback: { branch: string | null; base: string | null; message: string },
): MergeIssue {
  const record =
    body && typeof body === "object" ? (body as Record<string, unknown>) : {};
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
  const kind: MergeIssueKind = missingBranch
    ? "missing_branch"
    : record.conflict
      ? "conflict"
      : record.blocked
        ? "blocked"
        : "failed";
  return {
    kind,
    branch:
      typeof record.branch === "string" && record.branch
        ? record.branch
        : typeof detailRecord?.branch === "string" && detailRecord.branch
          ? detailRecord.branch
          : fallback.branch,
    base:
      typeof record.base === "string" && record.base
        ? record.base
        : typeof detailRecord?.base === "string" && detailRecord.base
          ? detailRecord.base
          : fallback.base,
    files: stringList(record.files),
    dirtyFiles: stringList(record.dirty_files),
    message:
      typeof record.message === "string" && record.message
        ? record.message
        : typeof errorRecord?.message === "string" && errorRecord.message
          ? errorRecord.message
        : fallback.message,
    reason,
  };
}

function issueTitle(kind: MergeIssueKind): string {
  if (kind === "missing_branch") return "分支已不存在，无法合并";
  if (kind === "conflict") return "合并冲突";
  if (kind === "blocked") return "合并被阻止";
  return "合并失败";
}

export function ReviewDetail({
  entry,
  onOpenTask,
}: {
  entry: ReviewEntry;
  onOpenTask: (id: string) => void;
}) {
  const { task, changes, workspace } = entry;
  const toast = useToast();
  const client = useQueryClient();
  const [activePath, setActivePath] = useState<string | null>(
    changes[0]?.rel_path ?? null,
  );
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [mergeConfirmOpen, setMergeConfirmOpen] = useState(false);
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false);
  const [rebuildConfirmOpen, setRebuildConfirmOpen] = useState(false);
  const [mergeIssue, setMergeIssue] = useState<MergeIssue | null>(null);
  const [discardedMessage, setDiscardedMessage] = useState<string | null>(null);

  const effectivePath =
    activePath && changes.some((change) => change.rel_path === activePath)
      ? activePath
      : changes[0]?.rel_path ?? null;
  const activeChange =
    changes.find((change) => change.rel_path === effectivePath) ?? null;
  const diff = useTaskDiff(task.id, effectivePath, Boolean(effectivePath));
  const fetchingChanges = useIsFetching({ queryKey: reviewKeys.changes(task.id) }) > 0;
  const fetchingWorkspace =
    useIsFetching({ queryKey: reviewKeys.workspace(task.id) }) > 0;
  const fetching = fetchingChanges || fetchingWorkspace || diff.isFetching;

  const textChanges = useMemo(
    () => changes.filter((change) => !change.is_binary).slice(0, MAX_SUMMARY_FILES),
    [changes],
  );
  const summaryQueries = useQueries({
    queries: textChanges.map((change) => taskDiffQuery(task.id, change.rel_path)),
  });

  let added = 0;
  let deleted = 0;
  let analyzed = 0;
  let pending = 0;
  for (const query of summaryQueries) {
    if (query.isLoading) pending += 1;
    const data = query.data;
    if (!data || data.binary) continue;
    const parsed = parseUnifiedDiff(data.diff ?? "");
    added += parsed.additions;
    deleted += parsed.deletions;
    analyzed += 1;
  }

  const binaryCount = changes.filter((change) => Boolean(change.is_binary)).length;
  const textTotal = changes.length - binaryCount;
  const unsyncedCount = workspace?.unsynced?.count ?? 0;
  const summaryHint =
    textTotal === 0
      ? "无文本改动"
      : analyzed < textTotal
        ? `${analyzed}/${textTotal} 已分析${pending ? " · 统计中" : ""}`
        : `${analyzed} 个文本文件`;

  const workspaceGit = workspace?.git ?? taskGitInfo(task);
  const mergeInfo = workspaceGit?.merge ?? null;
  const mergedBranch = mergeInfo?.branch ?? workspaceGit?.branch ?? null;
  const mergedSha = mergeInfo?.sha ?? workspaceGit?.commit ?? null;
  const mergeState: MergeState | null =
    workspace?.merge_status ?? taskMergeState(task);
  const isMerged = mergeState === "merged";
  const mergeDiscarded = mergeState === "discarded";
  const mergeMissingBranch = mergeState === "missing_branch";
  const mergeConflict = mergeState === "conflict";
  const mergeBlockedState =
    mergeConflict || mergeState === "blocked" || mergeState === "dirty_blocked";
  const mergeReady =
    mergeState === "auto_merge_disabled" ||
    mergeState === "not_merged" ||
    mergeState === "pending";
  const syncMode = mergeState === null;

  const mergeInfoCode =
    (mergeInfo as (MergeInfo & { code?: string | null }) | null)?.code ?? null;
  const derivedIssue: MergeIssue | null = mergeMissingBranch
    ? {
        kind: "missing_branch",
        branch: mergedBranch,
        base: mergeInfo?.base ?? workspaceGit?.base ?? null,
        files: [],
        dirtyFiles: [],
        message:
          mergeInfo?.error ||
          "任务分支已被删除，无法直接合并；可按记录的 commit/base 重建分支，或放弃任务。",
        reason: mergeInfo?.reason ?? mergeInfoCode ?? "missing_branch",
      }
    : mergeBlockedState
      ? {
          kind: mergeConflict ? "conflict" : "blocked",
          branch: mergedBranch,
          base: mergeInfo?.base ?? workspaceGit?.base ?? null,
          files: mergeInfo?.files ?? [],
          dirtyFiles: mergeInfo?.dirty_files ?? [],
          message:
            mergeInfo?.error ||
            (mergeInfo?.reason === "dirty_worktree"
              ? "基线工作树有未提交改动，已阻止合并；请先提交或暂存后再重试。"
              : mergeConflict
                ? "任务分支与基线存在冲突，合并已回滚，仓库保持干净。"
                : "合并被阻止，请检查基线状态后重试。"),
          reason: mergeInfo?.reason ?? null,
        }
      : null;
  const issue = mergeIssue ?? derivedIssue;
  const issueMissingBranch = issue?.kind === "missing_branch" || mergeMissingBranch;

  const sync = useSyncTask(task.id);
  const mergeTask = useMergeTask(task.id);
  const discard = useDiscardBranch(task.id);
  const rebuild = useRebuildBranch(task.id);
  const blockedReason =
    workspace?.can_sync === false
      ? workspace.sync_blocked_reason || "当前任务不可同步"
      : null;
  const canSync = changes.length > 0 && !blockedReason;

  const refresh = () => {
    setMergeIssue(null);
    void client.invalidateQueries({ queryKey: reviewKeys.changes(task.id) });
    void client.invalidateQueries({ queryKey: reviewKeys.workspace(task.id) });
    void client.invalidateQueries({ queryKey: ["review", "diff", task.id] });
  };

  const syncLabel = sync.isPending ? "同步中…" : `同步 ${changes.length} 个文件`;

  const confirmSync = () => {
    setConfirmOpen(false);
    sync.mutate(undefined, {
      onSuccess: (result) => {
        const committed = result.committed?.length ?? 0;
        const skipped = result.skipped?.length ?? 0;
        if (result.already_shared) {
          toast.info("改动已在工作区", result.message);
        } else if (committed === 0 && result.already_synced > 0) {
          toast.info("工作区已是最新", result.message);
        } else if (skipped > 0) {
          toast.info(`已同步 ${committed} 个文件，跳过 ${skipped} 个`, result.message);
        } else {
          toast.success(`已同步 ${committed} 个文件`, result.message);
        }
      },
      onError: (error) => {
        toast.error("同步失败", error instanceof Error ? error.message : String(error));
      },
    });
  };

  const runMerge = () => {
    mergeTask.mutate(undefined, {
      onSuccess: (result) => {
        setMergeIssue(null);
        setDiscardedMessage(null);
        const branch = result?.branch || mergedBranch;
        const sha = result?.sha ?? "";
        toast.success(
          branch
            ? `已合并到 ${branch}${sha ? ` @${sha.slice(0, 8)}` : ""}`
            : "合并成功",
          result?.message,
        );
      },
      onError: (error) => {
        const body = error instanceof ApiError ? error.body : null;
        const parsed = parseMergeIssue(body, {
          branch: issue?.branch ?? mergedBranch,
          base: issue?.base ?? workspaceGit?.base ?? null,
          message: error instanceof Error ? error.message : String(error),
        });
        // 409 只更新内联面板，不自动解决冲突、不猜测合并结果。
        setMergeIssue(parsed);
        toast.error(issueTitle(parsed.kind), parsed.message);
      },
    });
  };

  const confirmMerge = () => {
    setMergeConfirmOpen(false);
    runMerge();
  };

  const confirmDiscard = () => {
    setDiscardConfirmOpen(false);
    discard.mutate(undefined, {
      onSuccess: (result) => {
        setMergeIssue(null);
        const branch = result?.branch ? ` ${result.branch}` : "";
        setDiscardedMessage(
          result?.message || `任务分支${branch} 已删除，未合并内容无法找回。`,
        );
        toast.success(
          result?.branch_deleted ? `已删除任务分支${branch}` : "已放弃任务分支",
          result?.message,
        );
      },
      onError: (error) => {
        toast.error(
          "放弃任务分支失败",
          error instanceof Error ? error.message : String(error),
        );
      },
    });
  };

  const confirmRebuild = () => {
    setRebuildConfirmOpen(false);
    rebuild.mutate(undefined, {
      onSuccess: (result) => {
        setMergeIssue(null);
        setDiscardedMessage(null);
        const branch = result?.branch || mergedBranch;
        toast.success(
          "任务分支已重建",
          `${branch || "任务分支"}${result?.sha ? ` @${result.sha.slice(0, 8)}` : ""}`,
        );
        refresh();
      },
      onError: (error) => {
        toast.error(
          "重建分支失败",
          error instanceof Error ? error.message : String(error),
        );
      },
    });
  };

  return (
    <section className="card review-detail">
      <header className="review-detail-head">
        <div className="review-detail-title">
          <div className="review-detail-badges">
            <StatusBadge status={task.status} />
            <Badge tone="outline">{task.agent_id || "自动选择"}</Badge>
            <Badge tone="outline">{task.kind}</Badge>
            {workspace?.running ? <Badge tone="accent">仍在运行</Badge> : null}
            {isMerged ? (
              <Badge tone="success">
                已合并到 {mergedBranch || "基线"}
                {mergedSha ? ` @${mergedSha.slice(0, 8)}` : ""}
              </Badge>
            ) : null}
            {mergeDiscarded ? <Badge tone="warning">任务分支已放弃</Badge> : null}
          </div>
          <h2 className="review-detail-name">{task.title || "未命名任务"}</h2>
          <div className="review-detail-sub">
            <span className="mono">{task.id.slice(0, 8)}</span>
            <span className="dot-sep" />
            <span>更新于 {fmtRelative(task.updated_at)}</span>
            {workspace?.is_original ? (
              <>
                <span className="dot-sep" />
                <span>shared · 已直接写入工作区</span>
              </>
            ) : isMerged ? (
              <>
                <span className="dot-sep" />
                <span>改动已进入基线</span>
              </>
            ) : mergeDiscarded ? (
              <>
                <span className="dot-sep" />
                <span>任务分支已放弃，改动未合并</span>
              </>
            ) : issueMissingBranch ? (
              <>
                <span className="dot-sep" />
                <span>分支已不存在，无法合并</span>
              </>
            ) : mergeBlockedState ? (
              <>
                <span className="dot-sep" />
                <span>{mergeConflict ? "合并冲突待处理" : "合并被阻止"}</span>
              </>
            ) : mergeReady ? (
              <>
                <span className="dot-sep" />
                <span>尚未合并到基线</span>
              </>
            ) : unsyncedCount > 0 ? (
              <>
                <span className="dot-sep" />
                <span>待同步 {unsyncedCount} 项改动</span>
              </>
            ) : null}
          </div>
        </div>
        <div className="review-detail-actions">
          <Button size="sm" onClick={() => onOpenTask(task.id)}>
            <ExternalLink size={13} />
            打开任务
          </Button>
          <IconButton
            label="刷新改动"
            onClick={refresh}
            disabled={fetching}
            title="刷新改动"
          >
            <RefreshCw size={14} className={fetching ? "spin" : undefined} />
          </IconButton>
          {isMerged || mergeDiscarded ? null : issueMissingBranch ? (
            <Button
              size="sm"
              variant="primary"
              disabled
              title="任务分支已不存在，请先重建分支"
            >
              <GitMerge size={14} />
              合并到基线
            </Button>
          ) : issue ? null : mergeReady ? (
            <Button
              size="sm"
              variant="primary"
              disabled={mergeTask.isPending}
              onClick={() => setMergeConfirmOpen(true)}
            >
              <GitMerge size={14} />
              {mergeTask.isPending ? "合并中…" : "合并到基线"}
            </Button>
          ) : (
            <Button
              size="sm"
              variant="primary"
              disabled={!canSync || sync.isPending}
              title={blockedReason ?? undefined}
              onClick={() => setConfirmOpen(true)}
            >
              <CheckCheck size={14} />
              {syncLabel}
            </Button>
          )}
        </div>
      </header>

      {discardedMessage ? (
        <div className="review-merge-panel is-discarded">
          <div className="review-merge-panel-head">
            <Trash2 size={14} />
            <strong>已放弃任务分支</strong>
          </div>
          <p className="review-merge-message">{discardedMessage}</p>
          <div className="review-merge-actions">
            <Button size="sm" onClick={() => onOpenTask(task.id)}>
              <ExternalLink size={13} />
              打开任务
            </Button>
          </div>
        </div>
      ) : issue ? (
        <div className={clsx("review-merge-panel", `is-${issue.kind}`)} role="status">
          <div className="review-merge-panel-head">
            <TriangleAlert size={14} />
            <strong>{issueTitle(issue.kind)}</strong>
            {issue.branch ? (
              <span className="review-merge-branch mono">{issue.branch}</span>
            ) : null}
          </div>
          <dl className="review-merge-meta">
            <div>
              <dt>任务分支</dt>
              <dd className="mono">{issue.branch || "—"}</dd>
            </div>
            <div>
              <dt>基线</dt>
              <dd className="mono">{issue.base || "—"}</dd>
            </div>
            {issue.reason ? (
              <div>
                <dt>原因</dt>
                <dd>{issue.reason}</dd>
              </div>
            ) : null}
          </dl>
          {issue.files.length ? (
            <div className="review-merge-field">
              <span className="review-merge-field-label">冲突文件</span>
              <div className="review-merge-files">
                {issue.files.slice(0, MAX_ISSUE_FILES).map((file, index) => (
                  <span className="review-merge-file mono" key={`${file}-${index}`}>
                    {file}
                  </span>
                ))}
                {issue.files.length > MAX_ISSUE_FILES ? (
                  <span className="review-merge-file mono">
                    +{issue.files.length - MAX_ISSUE_FILES}
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}
          {issue.dirtyFiles.length ? (
            <div className="review-merge-field">
              <span className="review-merge-field-label">未提交文件</span>
              <div className="review-merge-files">
                {issue.dirtyFiles.slice(0, MAX_ISSUE_FILES).map((file, index) => (
                  <span
                    className="review-merge-file is-dirty mono"
                    key={`${file}-${index}`}
                  >
                    {file}
                  </span>
                ))}
                {issue.dirtyFiles.length > MAX_ISSUE_FILES ? (
                  <span className="review-merge-file is-dirty mono">
                    +{issue.dirtyFiles.length - MAX_ISSUE_FILES}
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}
          <p className="review-merge-message">{issue.message}</p>
          <div className="review-merge-actions">
            {issue.kind === "missing_branch" ? (
              <>
                <Button
                  size="sm"
                  variant="primary"
                  loading={rebuild.isPending}
                  onClick={() => setRebuildConfirmOpen(true)}
                >
                  <RefreshCw size={13} />
                  重建分支
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  loading={discard.isPending}
                  onClick={() => setDiscardConfirmOpen(true)}
                >
                  <Trash2 size={13} />
                  放弃任务
                </Button>
                <Button size="sm" variant="ghost" onClick={() => onOpenTask(task.id)}>
                  <ExternalLink size={13} />
                  打开任务
                </Button>
              </>
            ) : (
              <>
                <Button
                  size="sm"
                  variant="primary"
                  loading={mergeTask.isPending}
                  onClick={runMerge}
                >
                  <GitMerge size={13} />
                  重试合并
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  loading={discard.isPending}
                  onClick={() => setDiscardConfirmOpen(true)}
                >
                  <Trash2 size={13} />
                  放弃任务分支
                </Button>
                <Button size="sm" variant="ghost" onClick={() => onOpenTask(task.id)}>
                  <ExternalLink size={13} />
                  打开任务
                </Button>
              </>
            )}
          </div>
        </div>
      ) : null}

      <div className="review-metrics">
        <Metric label="改动文件" value={changes.length} hint={textTotal ? `${textTotal} 文本` : "无文本"} />
        <Metric
          label="新增行"
          value={textTotal ? `≈ +${added}` : "—"}
          hint={summaryHint}
          tone="add"
        />
        <Metric
          label="删除行"
          value={textTotal ? `≈ −${deleted}` : "—"}
          hint={summaryHint}
          tone="del"
        />
        <Metric label="二进制" value={binaryCount} hint={binaryCount ? "需人工查看" : "无二进制"} />
      </div>

      {syncMode && blockedReason ? (
        <div className="review-inline-note">
          <span>同步不可用：{blockedReason}</span>
        </div>
      ) : null}

      {syncMode && sync.isError ? (
        <div className="review-inline-error">
          <span>同步失败：{(sync.error as Error)?.message || String(sync.error)}</span>
        </div>
      ) : null}

      <div className="review-detail-body">
        <aside className="review-files">
          <div className="review-files-head">
            <span>文件</span>
            <Badge tone="outline">{changes.length}</Badge>
          </div>
          <div className="review-file-list">
            {changes.map((change) => (
              <FileRow
                key={`${change.rel_path}-${change.id}`}
                change={change}
                active={change.rel_path === effectivePath}
                onSelect={() => setActivePath(change.rel_path)}
              />
            ))}
          </div>
        </aside>

        <div className="review-diff-pane">
          {activeChange ? (
            <>
              <div className="review-diff-head">
                <span className="review-diff-path mono truncate" title={activeChange.rel_path}>
                  {activeChange.rel_path}
                </span>
                <div className="review-diff-meta">
                  <Badge tone={actionMeta(activeChange.action).tone}>
                    {actionMeta(activeChange.action).label}
                  </Badge>
                  <span>{fmtBytes(changeSize(activeChange))}</span>
                </div>
              </div>
              {diff.isLoading ? (
                <div className="review-loading">
                  <Spinner />
                  正在读取差异…
                </div>
              ) : diff.isError ? (
                <div className="review-empty-fill">
                  <EmptyState
                    title="无法读取该文件的差异"
                    hint={(diff.error as Error)?.message || "接口返回错误，可能是改动记录已过期。"}
                    action={
                      <Button size="sm" onClick={() => void diff.refetch()}>
                        重试
                      </Button>
                    }
                  />
                </div>
              ) : diff.data ? (
                <DiffViewer payload={diff.data} />
              ) : (
                <div className="review-empty-fill">
                  <EmptyState title="选择文件查看差异" hint="左侧文件列表中的改动会显示在这里。" />
                </div>
              )}
            </>
          ) : (
            <div className="review-empty-fill">
              <EmptyState title="没有文件改动" hint="该任务没有记录到可审阅的文件差异。" />
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title={`同步 ${changes.length} 个文件到工作区`}
        message={
          <div className="review-confirm">
            <p>
              将把 <strong>{task.title || "该任务"}</strong> 的工作副本改动写入项目根目录。
            </p>
            <p>同名文件会被覆盖；越权、保护路径或并发冲突的文件会被跳过。</p>
            {workspace?.root_path ? (
              <p className="mono review-confirm-path">{workspace.root_path}</p>
            ) : null}
          </div>
        }
        confirmLabel="确认同步"
        onCancel={() => setConfirmOpen(false)}
        onConfirm={confirmSync}
      />

      <ConfirmDialog
        open={mergeConfirmOpen}
        title="合并任务分支到基线"
        message={
          <div className="review-confirm">
            <p>
              将把 <strong>{task.title || "该任务"}</strong> 的任务分支
              {mergedBranch ? <span className="mono"> {mergedBranch}</span> : null}
              以 <strong>--no-ff</strong> 方式合并进基线分支。
            </p>
            <p>基线工作树必须干净：有未提交改动时合并会被阻止，不会覆盖你的改动。</p>
            {(mergeInfo?.base ?? workspaceGit?.base) ? (
              <p className="mono review-confirm-path">
                基线：{mergeInfo?.base ?? workspaceGit?.base}
              </p>
            ) : null}
          </div>
        }
        confirmLabel="确认合并"
        onCancel={() => setMergeConfirmOpen(false)}
        onConfirm={confirmMerge}
      />

      <ConfirmDialog
        open={rebuildConfirmOpen}
        title="重建任务分支"
        message={
          <div className="review-confirm">
            <p>
              将按 <strong>{task.title || "该任务"}</strong> 记录的 commit/base
              重新创建任务分支与工作副本。
            </p>
            <p>
              <strong>工作副本里未提交的内容可能丢失。</strong>
              重建后请重新检查改动，再执行合并。
            </p>
            {mergedBranch ? (
              <p className="mono review-confirm-path">任务分支：{mergedBranch}</p>
            ) : null}
          </div>
        }
        confirmLabel={rebuild.isPending ? "重建中…" : "确认重建"}
        onCancel={() => setRebuildConfirmOpen(false)}
        onConfirm={confirmRebuild}
      />

      <ConfirmDialog
        open={discardConfirmOpen}
        title="放弃任务分支"
        danger
        message={
          <div className="review-confirm">
            <p>
              将删除 <strong>{task.title || "该任务"}</strong> 的工作副本与任务分支
              {mergedBranch ? <span className="mono"> {mergedBranch}</span> : null}。
            </p>
            <p>
              <strong>未合并内容将不可从该分支找回。</strong>
            </p>
          </div>
        }
        confirmLabel="确认放弃分支"
        onCancel={() => setDiscardConfirmOpen(false)}
        onConfirm={confirmDiscard}
      />
    </section>
  );
}

function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string | number;
  hint: string;
  tone?: "add" | "del";
}) {
  return (
    <div className="review-metric">
      <span className="review-metric-label">{label}</span>
      <strong className={clsx("review-metric-value", tone && `is-${tone}`)}>{value}</strong>
      <span className="review-metric-hint">{hint}</span>
    </div>
  );
}

function FileRow({
  change,
  active,
  onSelect,
}: {
  change: ChangeRow;
  active: boolean;
  onSelect: () => void;
}) {
  const meta = actionMeta(change.action);
  const { name, dir } = splitPath(change.rel_path);
  return (
    <button
      type="button"
      className={clsx("review-file", active && "is-active")}
      title={change.rel_path}
      onClick={onSelect}
    >
      <span className={clsx("review-file-badge", `is-${meta.tone}`)}>{meta.badge}</span>
      <span className="review-file-main">
        <span className="review-file-name truncate">{name}</span>
        <span className="review-file-dir truncate">{dir || "根目录"}</span>
      </span>
      <span className="review-file-size">{fmtBytes(changeSize(change))}</span>
    </button>
  );
}
