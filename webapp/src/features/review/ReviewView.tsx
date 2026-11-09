import { useMemo, useState } from "react";
import { useQueries, useQueryClient, useIsFetching } from "@tanstack/react-query";
import { RefreshCw, Search, ShieldCheck } from "lucide-react";
import clsx from "clsx";
import { useDashboard } from "../../app/queries";
import { useUi } from "../../app/store";
import { fmtRelative } from "../../lib/format";
import { Badge, Button, EmptyState, Input, Spinner } from "../../ui/primitives";
import { taskMergeState } from "../task/helpers";
import { ReviewDetail } from "./ReviewDetail";
import {
  REVIEWABLE_STATUSES,
  groupFor,
  latestChanges,
  taskChangesQuery,
  taskWorkspaceQuery,
  type ReviewEntry,
  type ReviewGroupKey,
  type TaskWorkspace,
} from "./review-api";
import "./review.css";

const GROUP_ORDER: ReviewGroupKey[] = ["review", "synced", "failed"];

const GROUP_META: Record<ReviewGroupKey, { label: string; hint: string }> = {
  review: { label: "待审阅", hint: "等待人工确认后合并或同步" },
  synced: { label: "已合并 / 已同步", hint: "改动已在基线或工作目录" },
  failed: { label: "失败", hint: "可先检查产物再决定是否合并或同步" },
};

function taskTime(task: ReviewEntry["task"]): number {
  const value = Date.parse(task.updated_at);
  return Number.isNaN(value) ? 0 : value;
}

export function ReviewView({
  onOpenTask,
}: {
  onOpenTask?: (id: string) => void;
}) {
  const projectId = useUi((state) => state.projectId);
  const setView = useUi((state) => state.setView);
  const openTaskInStore = useUi((state) => state.openTask);
  const client = useQueryClient();
  const dashboard = useDashboard(projectId);
  const fetching = useIsFetching({ queryKey: ["review", "changes"] }) > 0;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  const candidates = useMemo(() => {
    const tasks = dashboard.data?.tasks ?? [];
    return tasks
      .filter((task) => REVIEWABLE_STATUSES.has(task.status))
      .sort((left, right) => taskTime(right) - taskTime(left));
  }, [dashboard.data?.tasks]);

  const changeQueries = useQueries({
    queries: candidates.map((task) => taskChangesQuery(task.id)),
  });

  const baseEntries = candidates.map((task, index) => ({
    task,
    changes: latestChanges(changeQueries[index]?.data),
    changesError: changeQueries[index]?.isError ?? false,
  }));

  const changedEntries = baseEntries.filter((entry) => entry.changes.length > 0);
  const workspaceQueries = useQueries({
    queries: changedEntries.map((entry) => taskWorkspaceQuery(entry.task.id)),
  });
  const workspaceById = new Map<
    string,
    { data?: TaskWorkspace; loading: boolean; error: boolean }
  >();
  changedEntries.forEach((entry, index) => {
    const query = workspaceQueries[index];
    workspaceById.set(entry.task.id, {
      data: query?.data,
      loading: query?.isLoading ?? false,
      error: query?.isError ?? false,
    });
  });

  const entries: ReviewEntry[] = baseEntries
    .filter((entry) => entry.changes.length > 0)
    .map((entry) => {
      const workspace = workspaceById.get(entry.task.id);
      return {
        task: entry.task,
        changes: entry.changes,
        workspace: workspace?.data,
        workspaceLoading: workspace?.loading ?? false,
        workspaceError: workspace?.error ?? false,
        group: groupFor(entry.task, workspace?.data),
      };
    });

  const groups = GROUP_ORDER.map((key) => ({
    key,
    rows: entries.filter((entry) => {
      if (entry.group !== key) return false;
      const needle = filter.trim().toLowerCase();
      if (!needle) return true;
      const haystack = [
        entry.task.title,
        entry.task.agent_id ?? "",
        ...entry.changes.map((change) => change.rel_path),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    }),
  })).filter((group) => group.rows.length > 0);

  const preferred =
    entries.find((entry) => entry.group === "review") ??
    entries.find((entry) => entry.group === "synced") ??
    entries[0] ??
    null;
  const selected = entries.find((entry) => entry.task.id === selectedId) ?? preferred;

  const totalFiles = entries.reduce((sum, entry) => sum + entry.changes.length, 0);
  const pendingTasks = entries.filter((entry) => entry.group === "review").length;
  const loadingChanges =
    candidates.length > 0 &&
    entries.length === 0 &&
    changeQueries.some((query) => query.isLoading);
  const changeErrors = baseEntries.filter((entry) => entry.changesError).length;

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["review"] });
    void client.invalidateQueries({ queryKey: ["dashboard"] });
  };

  const openTask = (id: string) => {
    if (onOpenTask) {
      onOpenTask(id);
      return;
    }
    setView("tasks");
    openTaskInStore(id);
  };

  if (!projectId) {
    return (
      <div className="page">
        <EmptyState
          title="先选择一个项目"
          hint="审阅队列会显示当前项目中等待确认的 Agent 产物。"
        />
      </div>
    );
  }

  return (
    <div className="page wide review-page">
      <div className="page-head review-page-head">
        <div className="page-head-text">
          <div className="review-eyebrow">REVIEW / 审阅</div>
          <h1 className="review-title">审阅队列</h1>
          <p className="page-subtitle">
            集中检查 Agent 产物，逐文件查看差异，确认后合并或同步到工作区。
          </p>
        </div>
        <div className="page-actions">
          <div className="review-head-stats">
            <span>
              <strong>{pendingTasks}</strong> 待审阅
            </span>
            <span>
              <strong>{totalFiles}</strong> 文件
            </span>
          </div>
          <Button size="sm" onClick={refresh} loading={fetching}>
            <RefreshCw size={13} />
            刷新队列
          </Button>
        </div>
      </div>

      {changeErrors > 0 ? (
        <div className="review-banner">
          {changeErrors} 个任务的改动读取失败，可能是接口暂不可用。
          <button type="button" onClick={refresh}>
            重试
          </button>
        </div>
      ) : null}

      {dashboard.isError ? (
        <div className="review-empty-page">
          <EmptyState
            title="无法加载审阅队列"
            hint={(dashboard.error as Error)?.message || "项目看板接口返回错误。"}
            action={
              <Button size="sm" onClick={() => void dashboard.refetch()}>
                重试
              </Button>
            }
          />
        </div>
      ) : loadingChanges || dashboard.isLoading ? (
        <div className="review-empty-page">
          <div className="review-loading review-loading-page">
            <Spinner />
            正在读取 Agent 改动…
          </div>
        </div>
      ) : entries.length === 0 ? (
        <div className="review-empty-page">
          <EmptyState
            icon={<ShieldCheck size={18} />}
            title="没有需要审阅的改动"
            hint="成功、失败或阻塞的任务产生文件改动后会出现在这里。合并或同步后任务会归入「已合并 / 已同步」。"
            action={
              <Button size="sm" onClick={() => setView("tasks")}>
                去任务看板
              </Button>
            }
          />
        </div>
      ) : (
        <div className="review-shell">
          <aside className="card review-sidebar">
            <div className="review-sidebar-head">
              <div className="review-search">
                <Search size={13} />
                <Input
                  className="review-search-input"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder="筛选任务或文件路径"
                  aria-label="筛选任务或文件路径"
                />
              </div>
              <div className="review-sidebar-sub">
                {entries.length} 个任务 · {totalFiles} 个文件
              </div>
            </div>

            <div className="review-list">
              {groups.length === 0 ? (
                <div className="review-filter-empty">没有匹配「{filter}」的任务</div>
              ) : (
                groups.map((group) => (
                  <div className="review-group" key={group.key}>
                    <div className="review-group-head">
                      <span className={clsx("review-group-dot", `is-${group.key}`)} />
                      <span className="review-group-label">{GROUP_META[group.key].label}</span>
                      <span className="review-group-count">{group.rows.length}</span>
                      <span className="review-group-hint">{GROUP_META[group.key].hint}</span>
                    </div>
                    {group.rows.map((entry) => (
                      <TaskItem
                        key={entry.task.id}
                        entry={entry}
                        active={selected?.task.id === entry.task.id}
                        onSelect={() => setSelectedId(entry.task.id)}
                      />
                    ))}
                  </div>
                ))
              )}
            </div>
          </aside>

          {selected ? (
            <ReviewDetail
              key={selected.task.id}
              entry={selected}
              onOpenTask={openTask}
            />
          ) : (
            <section className="card review-detail">
              <div className="review-empty-fill">
                <EmptyState
                  title="选择一个任务"
                  hint="左侧任务会显示文件改动、行数统计和统一差异。"
                />
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function TaskItem({
  entry,
  active,
  onSelect,
}: {
  entry: ReviewEntry;
  active: boolean;
  onSelect: () => void;
}) {
  const { task, changes, workspace } = entry;
  const pending = workspace?.unsynced?.count ?? 0;
  const mergeState = workspace?.merge_status ?? taskMergeState(task);
  const merged = mergeState === "merged";
  const mergeDiscarded = mergeState === "discarded";
  const mergeConflict = mergeState === "conflict";
  const mergeBlocked = mergeState === "blocked" || mergeState === "dirty_blocked";
  return (
    <button
      type="button"
      className={clsx("review-task", active && "is-active")}
      onClick={onSelect}
      title={task.title}
    >
      <span className="review-task-top">
        <span className="review-task-title truncate">{task.title || "未命名任务"}</span>
        <span className="review-task-count">{changes.length}</span>
      </span>
      <span className="review-task-meta">
        <span className="review-task-agent truncate">{task.agent_id || "自动选择"}</span>
        <span className="task-meta-dot" />
        <span>{fmtRelative(task.updated_at)}</span>
        {merged ? (
          <>
            <span className="task-meta-dot" />
            <Badge tone="success">已合并</Badge>
          </>
        ) : mergeDiscarded ? (
          <>
            <span className="task-meta-dot" />
            <Badge tone="warning">已放弃分支</Badge>
          </>
        ) : mergeConflict ? (
          <>
            <span className="task-meta-dot" />
            <Badge tone="warning">合并冲突</Badge>
          </>
        ) : mergeBlocked ? (
          <>
            <span className="task-meta-dot" />
            <Badge tone="warning">合并受阻</Badge>
          </>
        ) : pending > 0 ? (
          <>
            <span className="task-meta-dot" />
            <span className="review-task-pending">待同步 {pending} 项</span>
          </>
        ) : entry.group === "synced" ? (
          <>
            <span className="task-meta-dot" />
            <Badge tone="success">已同步</Badge>
          </>
        ) : null}
      </span>
    </button>
  );
}
