import { useDeferredValue, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Ban,
  Check,
  FilterX,
  FolderOpen,
  LayoutGrid,
  List,
  ListChecks,
  Minus,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  X,
} from "lucide-react";
import type { AgentUsage, Task, TaskStatus } from "../../api/types";
import { useDashboard, useInvalidateProject } from "../../app/queries";
import { useUi } from "../../app/store";
import { KIND_LABEL, STATUS_LABEL } from "../../lib/format";
import {
  Button,
  EmptyState,
  IconButton,
  Input,
  Panel,
  Segmented,
  Select,
} from "../../ui/primitives";
import { ConfirmDialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import { TaskCard, TaskListHeader, TaskListRow } from "./TaskItem";
import {
  BOARD_COLUMNS,
  groupByColumn,
  isActive,
  isRetryable,
  isRunnable,
  sortTasks,
  type SortMode,
  type ViewMode,
} from "./taskUtils";
import "./tasks.css";

const VIEW_STORAGE_KEY = "multiagent.tasks.view";
const BATCH_CONCURRENCY = 4;

type BatchOp = "run" | "retry" | "cancel";

interface BatchResult {
  id: string;
  ok: boolean;
  message?: string;
}

function loadViewMode(): ViewMode {
  try {
    return window.localStorage.getItem(VIEW_STORAGE_KEY) === "list" ? "list" : "board";
  } catch {
    return "board";
  }
}

function agentLabelsOf(agents: AgentUsage[], tasks: Task[]): Map<string, string> {
  const labels = new Map<string, string>();
  for (const agent of agents) {
    const id = agent.agent_id || agent.id;
    if (id) labels.set(id, agent.label || id);
  }
  for (const task of tasks) {
    const id = task.agent_id || "auto";
    if (!labels.has(id)) labels.set(id, id === "auto" ? "自动选择" : id);
  }
  return labels;
}

async function postTaskOp(taskId: string, op: BatchOp): Promise<void> {
  const response = await fetch(`/api/tasks/${taskId}/${op}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (response.ok) return;
  let detail = `${response.status} ${response.statusText}`.trim();
  try {
    const body = (await response.json()) as { detail?: unknown; error?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
    else if (typeof body.error === "string") detail = body.error;
  } catch {
    /* keep the HTTP status as the message */
  }
  throw new Error(detail);
}

async function mapLimit<T, R>(
  items: T[],
  limit: number,
  run: (item: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const index = cursor;
      cursor += 1;
      if (index >= items.length) return;
      results[index] = await run(items[index]);
    }
  });
  await Promise.all(workers);
  return results;
}

export function TasksView({ onOpenTask }: { onOpenTask: (id: string) => void }) {
  const projectId = useUi((state) => state.projectId);
  const setNewTaskOpen = useUi((state) => state.setNewTaskOpen);
  const dashboard = useDashboard(projectId);
  const invalidate = useInvalidateProject(projectId);
  const toast = useToast();

  const data = dashboard.data;
  const allTasks = useMemo(() => data?.tasks ?? [], [data]);

  const [view, setView] = useState<ViewMode>(loadViewMode);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [agentFilter, setAgentFilter] = useState("all");
  const [kindFilter, setKindFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sort, setSort] = useState<SortMode>("updated");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState<BatchOp | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    try {
      window.localStorage.setItem(VIEW_STORAGE_KEY, view);
    } catch {
      /* view preference is best-effort */
    }
  }, [view]);

  useEffect(() => {
    setSearch("");
    setAgentFilter("all");
    setKindFilter("all");
    setStatusFilter("all");
    setSelected(new Set());
  }, [projectId]);

  const hasRunning = allTasks.some(isActive);
  useEffect(() => {
    if (!hasRunning) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, [hasRunning]);

  const labels = useMemo(
    () => agentLabelsOf(data?.agents ?? [], allTasks),
    [data?.agents, allTasks],
  );

  const agentOptions = useMemo(
    () =>
      [...labels.entries()].sort((a, b) =>
        a[1].localeCompare(b[1], "zh-CN"),
      ),
    [labels],
  );

  const kindOptions = useMemo(() => {
    const kinds = new Set<string>();
    for (const task of allTasks) if (task.kind) kinds.add(task.kind);
    return [...kinds].sort((a, b) => a.localeCompare(b, "zh-CN"));
  }, [allTasks]);

  const statusOptions = useMemo(() => {
    const present = new Set<TaskStatus>(allTasks.map((task) => task.status));
    const ordered: TaskStatus[] = [];
    for (const column of BOARD_COLUMNS) {
      for (const status of column.statuses) {
        if (present.has(status) && !ordered.includes(status)) ordered.push(status);
      }
    }
    return ordered;
  }, [allTasks]);

  const filtered = useMemo(() => {
    const query = deferredSearch.trim().toLowerCase();
    return allTasks.filter((task) => {
      if (agentFilter !== "all" && (task.agent_id || "auto") !== agentFilter) return false;
      if (kindFilter !== "all" && task.kind !== kindFilter) return false;
      if (statusFilter !== "all" && task.status !== statusFilter) return false;
      if (!query) return true;
      const haystack = [
        task.title ?? "",
        task.brief ?? "",
        task.id,
        task.agent_id ?? "",
        ...(task.tags ?? []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [allTasks, deferredSearch, agentFilter, kindFilter, statusFilter]);

  const sorted = useMemo(() => sortTasks(filtered, sort), [filtered, sort]);
  const groups = useMemo(() => groupByColumn(sorted), [sorted]);

  useEffect(() => {
    setSelected((prev) => {
      if (!prev.size) return prev;
      let changed = false;
      const next = new Set<string>();
      for (const task of sorted) {
        if (prev.has(task.id)) next.add(task.id);
      }
      if (next.size !== prev.size) changed = true;
      return changed ? next : prev;
    });
  }, [sorted]);

  const filtersActive =
    search.trim().length > 0 ||
    agentFilter !== "all" ||
    kindFilter !== "all" ||
    statusFilter !== "all";

  const resetFilters = () => {
    setSearch("");
    setAgentFilter("all");
    setKindFilter("all");
    setStatusFilter("all");
  };

  const selectedTasks = useMemo(
    () => sorted.filter((task) => selected.has(task.id)),
    [sorted, selected],
  );
  const runnable = selectedTasks.filter(isRunnable);
  const retryable = selectedTasks.filter(isRetryable);
  const runningSelected = selectedTasks.filter(isActive);
  const allVisibleSelected =
    sorted.length > 0 && sorted.every((task) => selected.has(task.id));
  const someSelected = selected.size > 0;

  const toggleTask = (taskId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected((prev) => {
      const allSelected =
        sorted.length > 0 && sorted.every((task) => prev.has(task.id));
      if (allSelected) return new Set();
      return new Set(sorted.map((task) => task.id));
    });
  };

  const runBatch = async (op: BatchOp, tasks: Task[], verb: string) => {
    if (!tasks.length || busy) return;
    setBusy(op);
    try {
      const results = await mapLimit<Task, BatchResult>(tasks, BATCH_CONCURRENCY, async (task) => {
        try {
          await postTaskOp(task.id, op);
          return { id: task.id, ok: true };
        } catch (error) {
          return { id: task.id, ok: false, message: (error as Error).message };
        }
      });

      const failures = results.filter((result) => !result.ok);
      const done = results.filter((result) => result.ok).map((result) => result.id);
      if (done.length) {
        setSelected((prev) => {
          const next = new Set(prev);
          for (const id of done) next.delete(id);
          return next;
        });
      }

      if (failures.length) {
        const detail =
          failures
            .slice(0, 3)
            .map((failure) => failure.message)
            .filter(Boolean)
            .join("；") || undefined;
        toast.error(
          `${done.length} 个任务已${verb}，${failures.length} 个失败`,
          detail,
        );
      } else {
        toast.success(`${done.length} 个任务已${verb}`);
      }
      invalidate();
    } finally {
      setBusy(null);
    }
  };

  const total = allTasks.length;
  const runningCount = allTasks.filter(isActive).length;
  const attentionCount = allTasks.filter(
    (task) => task.status === "waiting" || task.status === "failed" || task.status === "blocked",
  ).length;

  const head = (
    <div className="page-head">
      <div className="page-head-text">
        <h1>任务</h1>
        <div className="page-subtitle tk-summary">
          <span>共 {total} 个任务</span>
          <span className="tk-summary-dot" />
          <span>{runningCount} 个执行中</span>
          <span className="tk-summary-dot" />
          <span className={attentionCount ? "tk-summary-warn" : undefined}>
            {attentionCount} 个待处理
          </span>
        </div>
      </div>
      <div className="page-actions">
        <Segmented<ViewMode>
          value={view}
          onChange={setView}
          ariaLabel="任务视图"
          options={[
            {
              value: "board",
              label: (
                <>
                  <LayoutGrid size={13} /> 看板
                </>
              ),
              title: "按状态分列",
            },
            {
              value: "list",
              label: (
                <>
                  <List size={13} /> 列表
                </>
              ),
              title: "紧凑列表",
            },
          ]}
        />
        <IconButton
          label="刷新任务"
          onClick={() => void dashboard.refetch()}
          disabled={dashboard.isFetching}
        >
          <RefreshCw size={14} className={dashboard.isFetching ? "spin" : undefined} />
        </IconButton>
        <Button variant="primary" onClick={() => setNewTaskOpen(true)}>
          <Plus size={14} /> 新建任务
        </Button>
      </div>
    </div>
  );

  if (!projectId) {
    return (
      <div className="page wide tasks-page">
        {head}
        <Panel className="tk-empty-panel">
          <EmptyState
            icon={<FolderOpen size={18} />}
            title="还没有选择项目"
            hint="在左上角选择或导入一个工作区，任务会显示在这里。"
          />
        </Panel>
      </div>
    );
  }

  if (dashboard.isLoading) {
    return <TasksSkeleton />;
  }

  if (dashboard.isError) {
    const message =
      dashboard.error instanceof Error ? dashboard.error.message : "无法加载任务数据";
    return (
      <div className="page wide tasks-page">
        {head}
        <Panel className="tk-empty-panel">
          <EmptyState
            icon={<AlertTriangle size={18} />}
            title="任务加载失败"
            hint={message}
            action={
              <Button onClick={() => void dashboard.refetch()}>
                <RefreshCw size={14} /> 重试
              </Button>
            }
          />
        </Panel>
      </div>
    );
  }

  return (
    <div className="page wide tasks-page">
      {head}

      <div className="tk-toolbar" role="search" aria-label="任务筛选">
        <div className="tk-search">
          <Search size={14} />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索标题、标签、Agent…"
            aria-label="搜索任务"
            spellCheck={false}
          />
          {search ? (
            <button
              type="button"
              className="tk-search-clear"
              aria-label="清除搜索"
              onClick={() => setSearch("")}
            >
              <X size={13} />
            </button>
          ) : null}
        </div>

        <Select
          className="tk-select"
          aria-label="按 Agent 筛选"
          value={agentFilter}
          onChange={(event) => setAgentFilter(event.target.value)}
        >
          <option value="all">全部 Agent</option>
          {agentOptions.map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </Select>

        <Select
          className="tk-select"
          aria-label="按任务类型筛选"
          value={kindFilter}
          onChange={(event) => setKindFilter(event.target.value)}
        >
          <option value="all">全部类型</option>
          {kindOptions.map((kind) => (
            <option key={kind} value={kind}>
              {KIND_LABEL[kind] ?? kind}
            </option>
          ))}
        </Select>

        <Select
          className="tk-select"
          aria-label="按状态筛选"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
        >
          <option value="all">全部状态</option>
          {statusOptions.map((status) => (
            <option key={status} value={status}>
              {STATUS_LABEL[status] ?? status}
            </option>
          ))}
        </Select>

        <Select
          className="tk-select tk-select-wide"
          aria-label="任务排序"
          value={sort}
          onChange={(event) => setSort(event.target.value as SortMode)}
        >
          <option value="updated">按更新时间</option>
          <option value="priority">按优先级</option>
        </Select>

        {filtersActive ? (
          <Button variant="ghost" size="sm" onClick={resetFilters}>
            <FilterX size={13} /> 清除筛选
          </Button>
        ) : null}

        <span className="tk-result" aria-live="polite">
          {filtersActive ? `${sorted.length} / ${total} 个任务` : `${total} 个任务`}
        </span>
      </div>

      {total === 0 ? (
        <Panel className="tk-empty-panel">
          <EmptyState
            icon={<ListChecks size={18} />}
            title="还没有任务"
            hint="描述一个目标，工作台会把它拆成任务并分配给 Agent。也可以先导入一个已有项目。"
            action={
              <Button variant="primary" onClick={() => setNewTaskOpen(true)}>
                <Plus size={14} /> 新建第一个任务
              </Button>
            }
          />
        </Panel>
      ) : sorted.length === 0 ? (
        <Panel className="tk-empty-panel">
          <EmptyState
            icon={<FilterX size={18} />}
            title="没有匹配的任务"
            hint="当前筛选条件下没有任务。清除筛选或换一个关键词试试。"
            action={
              <Button onClick={resetFilters}>
                <FilterX size={14} /> 清除筛选
              </Button>
            }
          />
        </Panel>
      ) : (
        <>
          <div className="tk-viewbar">
            <button
              type="button"
              role="checkbox"
              aria-checked={allVisibleSelected ? true : someSelected ? "mixed" : false}
              className="tk-selectall"
              onClick={toggleAll}
              disabled={busy !== null}
            >
              <span className="tk-selectall-box" aria-hidden="true">
                {allVisibleSelected ? <Check size={11} /> : someSelected ? <Minus size={11} /> : null}
              </span>
              {allVisibleSelected ? "取消全选" : "全选"}
            </button>
            <span className="tk-viewbar-hint">
              {view === "board" ? "按状态分列，点卡片查看详情" : "点击行打开任务详情"}
            </span>
          </div>

          {selectedTasks.length ? (
            <div className="tk-bulkbar" role="region" aria-label="批量操作">
              <span className="tk-bulk-count">已选 {selectedTasks.length} 个</span>
              <div className="tk-bulk-actions">
                <Button
                  size="sm"
                  onClick={() => void runBatch("run", runnable, "启动")}
                  disabled={!runnable.length || busy !== null}
                  loading={busy === "run"}
                  title="只有待执行或已中断的任务可以启动"
                >
                  <Play size={13} /> 运行所选
                </Button>
                <Button
                  size="sm"
                  onClick={() => void runBatch("retry", retryable, "重试")}
                  disabled={!retryable.length || busy !== null}
                  loading={busy === "retry"}
                  title="重试失败、阻塞或已中断的任务"
                >
                  <RotateCcw size={13} /> 重试失败
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => setConfirmCancel(true)}
                  disabled={!runningSelected.length || busy !== null}
                  loading={busy === "cancel"}
                  title="取消正在执行的任务"
                >
                  <Ban size={13} /> 取消运行
                </Button>
              </div>
              <Button
                size="sm"
                variant="ghost"
                className="tk-bulk-clear"
                onClick={() => setSelected(new Set())}
                disabled={busy !== null}
              >
                <X size={13} /> 清除选择
              </Button>
            </div>
          ) : null}

          {view === "board" ? (
            <div className="tk-board-scroll">
              <div className="tk-board">
                {BOARD_COLUMNS.map((column) => {
                  const tasks = groups[column.key] ?? [];
                  return (
                    <section key={column.key} className="tk-col" aria-label={column.label}>
                      <header className="tk-col-head">
                        <span
                          className={`tk-col-dot is-${column.tone}`}
                          aria-hidden="true"
                        />
                        <span className="tk-col-name">{column.label}</span>
                        <span className="tk-col-count" aria-label={`${tasks.length} 个任务`}>
                          {tasks.length}
                        </span>
                      </header>
                      <div className="tk-col-body">
                        {tasks.map((task) => (
                          <TaskCard
                            key={task.id}
                            task={task}
                            agentLabel={
                              labels.get(task.agent_id || "auto") ??
                              task.agent_id ??
                              "自动选择"
                            }
                            now={now}
                            selected={selected.has(task.id)}
                            onOpen={onOpenTask}
                            onToggle={toggleTask}
                          />
                        ))}
                        {tasks.length === 0 ? (
                          <div className="tk-col-empty">{column.empty}</div>
                        ) : null}
                      </div>
                    </section>
                  );
                })}
              </div>
            </div>
          ) : (
            <div className="tk-list" aria-label="任务列表">
              <TaskListHeader />
              {sorted.map((task) => (
                <TaskListRow
                  key={task.id}
                  task={task}
                  agentLabel={
                    labels.get(task.agent_id || "auto") ?? task.agent_id ?? "自动选择"
                  }
                  now={now}
                  selected={selected.has(task.id)}
                  onOpen={onOpenTask}
                  onToggle={toggleTask}
                />
              ))}
            </div>
          )}
        </>
      )}

      <ConfirmDialog
        open={confirmCancel}
        title="取消正在运行的任务"
        message={
          <>
            将取消 <strong>{runningSelected.length}</strong> 个正在执行的任务。已产生的产物会保留，
            任务会被标记为「已取消」。确定继续吗？
          </>
        }
        confirmLabel="取消任务"
        danger
        onCancel={() => setConfirmCancel(false)}
        onConfirm={() => {
          setConfirmCancel(false);
          void runBatch("cancel", runningSelected, "取消");
        }}
      />
    </div>
  );
}

function TasksSkeleton() {
  return (
    <div className="page wide tasks-page">
      <div className="page-head">
        <div className="page-head-text">
          <div className="skeleton tk-skel-title" />
          <div className="skeleton tk-skel-sub" />
        </div>
      </div>
      <div className="tk-skel-toolbar">
        <div className="skeleton tk-skel-input" />
        {[0, 1, 2, 3].map((index) => (
          <div key={index} className="skeleton tk-skel-select" />
        ))}
      </div>
      <div className="tk-board-scroll">
        <div className="tk-board">
          {BOARD_COLUMNS.map((column) => (
            <div key={column.key} className="tk-skel-col">
              <div className="skeleton tk-skel-colhead" />
              <div className="skeleton tk-skel-card" />
              <div className="skeleton tk-skel-card" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
