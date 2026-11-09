import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  AlertTriangle,
  ArrowRight,
  Binary,
  Copy,
  FileDiff,
  FilePlus2,
  FileX2,
  GitMerge,
  Loader2,
  RefreshCw,
  UploadCloud,
  WrapText,
} from "lucide-react";
import { ApiError, api, qk } from "../../api/client";
import type { ChangeRow, DiffPayload, Task, TaskDetail } from "../../api/types";
import { Badge, Button, EmptyState } from "../../ui/primitives";
import { ConfirmDialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import { useLocalStorage } from "../../lib/hooks";
import { fmtBytes, fmtDateTime } from "../../lib/format";
import { fromApiHunks, parseUnifiedDiff, type DiffLine, type ParsedDiff, type ParsedHunk } from "./diff";
import {
  changeSymbol,
  copyText,
  dirName,
  fileName,
  taskGitInfo,
  taskMergeInfo,
  taskMergeState,
} from "./helpers";

interface SyncResponse {
  message?: string;
  committed?: string[];
  skipped?: string[];
  skipped_details?: Array<{ rel_path: string; reason: string }>;
  already_shared?: boolean;
  conflicts?: string[];
  out_of_scope?: string[];
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
  committed?: string[];
  count?: number;
}

function actionTone(action: string | undefined): string {
  const normalized = String(action ?? "").toLowerCase();
  if (["create", "add", "added"].includes(normalized)) return "is-add";
  if (["delete", "remove", "deleted"].includes(normalized)) return "is-del";
  if (["rename", "move"].includes(normalized)) return "is-move";
  return "is-mod";
}

function sizeLabel(change: ChangeRow): string {
  const before = change.before_size;
  const after = change.after_size;
  if (change.action === "create") return fmtBytes(after ?? 0);
  if (change.action === "delete") return fmtBytes(before ?? 0);
  if (before === null || before === undefined) return fmtBytes(after ?? 0);
  if (after === null || after === undefined) return fmtBytes(before);
  return `${fmtBytes(before)} → ${fmtBytes(after)}`;
}

function DiffLineRow({ line }: { line: DiffLine }) {
  if (line.type === "meta") {
    return <div className="td-diff-line is-meta">{line.text}</div>;
  }
  const sign = line.type === "add" ? "+" : line.type === "del" ? "−" : " ";
  return (
    <div className={`td-diff-line is-${line.type}`}>
      <span className="td-diff-no">{line.oldNo ?? ""}</span>
      <span className="td-diff-no">{line.newNo ?? ""}</span>
      <span className="td-diff-sign">{sign}</span>
      <span className="td-diff-code">{line.text || " "}</span>
    </div>
  );
}

function HunkBlock({ hunk }: { hunk: ParsedHunk }) {
  const [expanded, setExpanded] = useState(false);
  const collapsible = hunk.lines.length > 34;
  const showAll = expanded || !collapsible;
  const head = hunk.lines.slice(0, 18);
  const tail = hunk.lines.slice(-8);
  const hidden = hunk.lines.length - head.length - tail.length;
  return (
    <div className="td-hunk">
      <div className="td-hunk-head">
        <span className="mono">{hunk.header}</span>
        <span className="td-hunk-count">
          <span className="is-add">+{hunk.additions}</span>
          <span className="is-del">−{hunk.deletions}</span>
        </span>
      </div>
      <div className="td-hunk-lines">
        {showAll
          ? hunk.lines.map((line, index) => <DiffLineRow key={index} line={line} />)
          : (
              <>
                {head.map((line, index) => (
                  <DiffLineRow key={`h-${index}`} line={line} />
                ))}
                <button
                  type="button"
                  className="td-hunk-more"
                  onClick={() => setExpanded(true)}
                >
                  展开 {hidden} 行
                </button>
                {tail.map((line, index) => (
                  <DiffLineRow key={`t-${index}`} line={line} />
                ))}
              </>
            )}
      </div>
      {showAll && collapsible ? (
        <button type="button" className="td-hunk-collapse" onClick={() => setExpanded(false)}>
          收起长片段
        </button>
      ) : null}
    </div>
  );
}

function countDiff(diff: ParsedDiff | null | undefined): { additions: number; deletions: number } {
  return {
    additions: diff?.additions ?? 0,
    deletions: diff?.deletions ?? 0,
  };
}

export function ChangesTab({
  detail,
  taskId,
  task: taskProp,
  onSynced,
}: {
  detail: TaskDetail;
  taskId: string;
  task?: Task;
  onSynced?: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const task = taskProp ?? detail.task;
  const changes = useMemo(
    () => [...(detail.changes ?? [])].sort((left, right) => right.id - left.id),
    [detail.changes],
  );
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [wrapped, setWrapped] = useLocalStorage("multiagent.diff.wrap", false);
  const [syncing, setSyncing] = useState(false);
  const [confirmSync, setConfirmSync] = useState(false);
  const [merging, setMerging] = useState(false);
  const [confirmMerge, setConfirmMerge] = useState(false);

  const mergeState = taskMergeState(task);
  const mergeInfo = taskMergeInfo(task);
  const gitInfo = taskGitInfo(task);
  const mergedBranch = mergeInfo?.branch || gitInfo?.branch || "";
  const mergeBlocked =
    mergeState === "conflict" || mergeState === "dirty_blocked" || mergeState === "blocked";
  const mergeMissingBranch = mergeState === "missing_branch";
  const canMerge =
    mergeState === "auto_merge_disabled" ||
    mergeState === "not_merged" ||
    mergeState === "pending";

  useEffect(() => {
    if (!changes.length) {
      if (selectedPath !== null) setSelectedPath(null);
      return;
    }
    if (!selectedPath || !changes.some((change) => change.rel_path === selectedPath)) {
      setSelectedPath(changes[0].rel_path);
    }
  }, [changes, selectedPath]);

  const selected = changes.find((change) => change.rel_path === selectedPath) ?? null;

  const diffQuery = useQuery({
    queryKey: ["task-diff", taskId, selectedPath ?? ""],
    queryFn: () =>
      api.get<DiffPayload>(
        `/api/tasks/${taskId}/diff?path=${encodeURIComponent(selectedPath ?? "")}`,
      ),
    enabled: Boolean(selectedPath),
    retry: false,
  });

  const diff = diffQuery.data;
  const parsed = useMemo<ParsedDiff>(() => {
    if (!diff) return { hunks: [], additions: 0, deletions: 0, hadHeaders: false };
    if (Array.isArray(diff.hunks) && diff.hunks.length) return fromApiHunks(diff.hunks);
    return parseUnifiedDiff(diff.diff);
  }, [diff]);
  const totals = countDiff(parsed);
  const canSync =
    mergeState === null &&
    task.status === "success" &&
    !task.read_only &&
    task.isolation !== "shared";

  const runSync = async () => {
    setConfirmSync(false);
    setSyncing(true);
    try {
      const result = await api.post<SyncResponse>(`/api/tasks/${taskId}/sync`, {});
      const skipped = result.skipped?.length ?? 0;
      const committed = result.committed?.length ?? 0;
      toast.success(
        result.message || `已同步 ${committed} 个文件到工作区`,
        skipped ? `${skipped} 个文件被跳过（冲突、越界或受保护）` : undefined,
      );
      void client.invalidateQueries({ queryKey: qk.task(taskId) });
      void client.invalidateQueries({ queryKey: ["task-diff", taskId] });
      onSynced?.();
    } catch (error) {
      toast.error("同步失败", (error as Error).message);
    } finally {
      setSyncing(false);
    }
  };

  const runMerge = async () => {
    setConfirmMerge(false);
    setMerging(true);
    try {
      const result = await api.post<MergeResponse>(`/api/tasks/${taskId}/merge`, {});
      const branch = result.branch || mergedBranch || "基线";
      const sha = String(result.sha || "").slice(0, 8);
      toast.success(
        `已合并到 ${branch}${sha ? `（${sha}）` : ""}`,
        result.files?.length ? `${result.files.length} 个文件` : undefined,
      );
      void client.invalidateQueries({ queryKey: qk.task(taskId) });
      void client.invalidateQueries({ queryKey: ["task-diff", taskId] });
      onSynced?.();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        const body =
          error.body && typeof error.body === "object"
            ? (error.body as Record<string, unknown>)
            : null;
        const detail =
          typeof body?.message === "string" && body.message ? body.message : error.message;
        toast.error("合并被阻止", `${detail}；请到任务概览的「合并问题」面板处理。`);
        void client.invalidateQueries({ queryKey: qk.task(taskId) });
        onSynced?.();
        return;
      }
      toast.error("合并失败", (error as Error).message);
    } finally {
      setMerging(false);
    }
  };

  const viewMergeIssue = () => {
    onSynced?.();
    toast.info("合并问题在任务概览处理", "页头下方有「合并问题」面板。");
  };

  const copyPath = async (path: string) => {
    const ok = await copyText(path);
    if (ok) toast.success("已复制路径", path);
    else toast.error("复制失败");
  };

  return (
    <div className="td-changes">
      <div className="td-changes-head">
        <div className="td-changes-summary">
          <span className="td-changes-title">
            <FileDiff size={15} /> 文件改动
            <Badge tone="outline">{changes.length}</Badge>
          </span>
          <span className="td-changes-sub">
            {changes.length
              ? changes[0]?.created_at
                ? `最近更新 ${fmtDateTime(changes[0].created_at)}`
                : `${changes.length} 个文件已记录`
              : "任务运行后，Agent 写入的文件会出现在这里"}
          </span>
        </div>
        <div className="td-changes-actions">
          {mergeState === "merged" ? (
            <span className="td-changes-merge-note">
              <GitMerge size={13} /> 已合并到分支{" "}
              <code className="mono">{mergedBranch || "基线"}</code>，无需同步
            </span>
          ) : null}
          {mergeState === "discarded" ? (
            <span className="td-changes-merge-hint">
              <GitMerge size={13} /> 任务分支已放弃，未合并任何改动
            </span>
          ) : null}
          {mergeMissingBranch ? (
            <>
              <span className="td-changes-merge-hint">
                <AlertTriangle size={13} /> 分支已不存在，无法合并
              </span>
              <Button size="sm" disabled title="请先在任务详情中重建分支">
                <GitMerge size={13} /> 合并到基线
              </Button>
            </>
          ) : null}
          {mergeBlocked ? (
            <>
              <span className="td-changes-merge-hint">
                <AlertTriangle size={13} /> 合并被阻止
              </span>
              <Button size="sm" onClick={viewMergeIssue}>
                查看合并问题
              </Button>
            </>
          ) : null}
          {canMerge ? (
            <Button
              size="sm"
              variant="primary"
              loading={merging}
              onClick={() => setConfirmMerge(true)}
            >
              <GitMerge size={13} /> 合并到基线
            </Button>
          ) : null}
          {mergeState === null && !canSync && changes.length ? (
            <Badge tone="outline">
              {task.isolation === "shared" ? "共享工作区，无需同步" : "任务成功后可直接同步"}
            </Badge>
          ) : null}
          {mergeState === null && canSync ? (
            <Button
              size="sm"
              variant="primary"
              loading={syncing}
              onClick={() => setConfirmSync(true)}
              disabled={!changes.length}
            >
              <UploadCloud size={13} /> 同步到工作区
            </Button>
          ) : null}
        </div>
      </div>

      {changes.length ? (
        <div className="td-changes-layout">
          <aside className="td-changes-list-wrap">
            <ul className="td-changes-list">
              {changes.map((change) => {
                const symbol = changeSymbol(change.action);
                return (
                  <li key={`${change.id}-${change.rel_path}`}>
                    <div
                      className={clsx(
                        "td-change-row",
                        selectedPath === change.rel_path && "is-active",
                      )}
                    >
                      <button
                        type="button"
                        className="td-change-main"
                        onClick={() => setSelectedPath(change.rel_path)}
                        title={change.rel_path}
                      >
                        <span className={clsx("td-change-symbol", actionTone(change.action))}>
                          {symbol.symbol}
                        </span>
                        <span className="td-change-text">
                          <span className="td-change-name">{fileName(change.rel_path)}</span>
                          <span className="td-change-dir">{dirName(change.rel_path) || "."}</span>
                        </span>
                        <span className="td-change-size">{sizeLabel(change)}</span>
                      </button>
                      <button
                        type="button"
                        className="td-icon-btn"
                        aria-label={`复制路径 ${change.rel_path}`}
                        onClick={() => void copyPath(change.rel_path)}
                      >
                        <Copy size={12} />
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </aside>

          <section className="td-diff-panel">
            {selected ? (
              <>
                <div className="td-diff-head">
                  <div className="td-diff-title">
                    <span className={clsx("td-diff-file-icon", actionTone(selected.action))}>
                      {selected.action === "create" ? (
                        <FilePlus2 size={15} />
                      ) : selected.action === "delete" ? (
                        <FileX2 size={15} />
                      ) : (
                        <FileDiff size={15} />
                      )}
                    </span>
                    <div className="td-diff-path">
                      <div className="td-diff-filename">{fileName(selected.rel_path)}</div>
                      <div className="td-diff-fullpath mono" title={selected.rel_path}>
                        {selected.rel_path}
                      </div>
                    </div>
                  </div>
                  <div className="td-diff-actions">
                    {diff && !diff.binary ? (
                      <span className="td-diff-counts">
                        <span className="is-add">+{totals.additions}</span>
                        <span className="is-del">−{totals.deletions}</span>
                      </span>
                    ) : null}
                    <button
                      type="button"
                      className={clsx("td-icon-btn", wrapped && "is-active")}
                      aria-label={wrapped ? "关闭自动换行" : "开启自动换行"}
                      title={wrapped ? "关闭自动换行" : "自动换行"}
                      onClick={() => setWrapped((value) => !value)}
                    >
                      <WrapText size={14} />
                    </button>
                    <button
                      type="button"
                      className="td-icon-btn"
                      aria-label="复制路径"
                      title="复制路径"
                      onClick={() => void copyPath(selected.rel_path)}
                    >
                      <Copy size={14} />
                    </button>
                    <button
                      type="button"
                      className="td-icon-btn"
                      aria-label="刷新差异"
                      title="刷新"
                      onClick={() => void diffQuery.refetch()}
                    >
                      <RefreshCw size={14} className={diffQuery.isFetching ? "spin" : undefined} />
                    </button>
                  </div>
                </div>

                <div className="td-diff-meta">
                  <Badge tone={selected.action === "delete" ? "danger" : "outline"}>
                    {changeSymbol(selected.action).label}
                  </Badge>
                  <span>{sizeLabel(selected)}</span>
                  {diff?.binary || selected.is_binary ? (
                    <Badge tone="warning">
                      <Binary size={11} /> 二进制
                    </Badge>
                  ) : null}
                  <span className="td-diff-meta-time">{fmtDateTime(selected.created_at)}</span>
                </div>

                {diffQuery.isLoading ? (
                  <div className="td-diff-loading">
                    <Loader2 size={16} className="spin" /> 正在读取差异…
                  </div>
                ) : diffQuery.isError ? (
                  <div className="td-diff-error">
                    <AlertTriangle size={16} />
                    <div>
                      <div>无法读取这个文件的差异</div>
                      <div className="td-diff-error-detail">
                        {(diffQuery.error as Error)?.message || "接口不可用"}
                      </div>
                    </div>
                    <Button size="sm" onClick={() => void diffQuery.refetch()}>
                      重试
                    </Button>
                  </div>
                ) : diff?.binary ? (
                  <div className="td-diff-binary">
                    <span className="td-binary-icon">
                      <Binary size={26} />
                    </span>
                    <div className="td-binary-title">二进制文件，无法展示文本差异</div>
                    <div className="td-binary-hint">
                      {diff.note || "仅记录大小与摘要，合并需人工处理。"}
                    </div>
                    <div className="td-binary-sizes">
                      <span>之前 {fmtBytes(diff.before_size ?? selected.before_size ?? 0)}</span>
                      <ArrowRight size={12} />
                      <span>之后 {fmtBytes(diff.after_size ?? selected.after_size ?? 0)}</span>
                    </div>
                  </div>
                ) : parsed.hunks.length ? (
                  <div className={clsx("td-diff-lines", wrapped && "is-wrap")}>
                    {parsed.hunks.map((hunk, index) => (
                      <HunkBlock key={`${hunk.header}-${index}`} hunk={hunk} />
                    ))}
                  </div>
                ) : (
                  <div className="td-diff-empty">
                    <FileDiff size={22} />
                    <div>没有可展示的文本差异</div>
                    <div className="td-diff-empty-hint">
                      {diff?.note || "可能是二进制、仅权限变化，或改动已回滚。"}
                    </div>
                    {diff?.diff ? (
                      <pre className="td-diff-raw">{diff.diff}</pre>
                    ) : null}
                  </div>
                )}
              </>
            ) : (
              <EmptyState
                icon={<FileDiff size={17} />}
                title="选择一个文件"
                hint="左侧列出这次任务改动过的所有文件。"
              />
            )}
          </section>
        </div>
      ) : (
        <EmptyState
          icon={<FileDiff size={20} />}
          title="还没有文件改动"
          hint="任务开始执行后，Agent 在隔离副本里写入的每个文件都会记录在这里，并保留可回看的差异。"
        />
      )}

      <ConfirmDialog
        open={confirmSync}
        title="同步改动到工作区"
        message={
          <>
            将把工作副本中的 <strong>{changes.length}</strong> 个文件写入项目工作区。只同步
            write_scope 内、未冲突且非受保护的路径；越界文件会被跳过。
          </>
        }
        confirmLabel={syncing ? "同步中…" : "确认同步"}
        onCancel={() => setConfirmSync(false)}
        onConfirm={() => void runSync()}
      />

      <ConfirmDialog
        open={confirmMerge}
        title="合并到基线？"
        message={
          <>
            将通过 <code>--no-ff</code> 把任务分支合并到项目当前基线分支；合并前基线工作树必须干净。
            冲突不会自动解决，系统会中止合并并保持仓库干净，请到工作目录手动解决后重试。
          </>
        }
        confirmLabel={merging ? "合并中…" : "确认合并"}
        onCancel={() => setConfirmMerge(false)}
        onConfirm={() => void runMerge()}
      />
    </div>
  );
}
