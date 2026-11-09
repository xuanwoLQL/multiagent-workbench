import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  RefreshCw,
  Search,
  Square,
} from "lucide-react";
import clsx from "clsx";
import { useUi } from "../../app/store";
import { useDashboard } from "../../app/queries";
import { Badge, Button, EmptyState, IconButton, Input, Panel, Select, Stat } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { fmtDuration, fmtInt } from "../../lib/format";
import { RUN_STATUS_LABEL, useRuns, useStopProject } from "./runs-api";
import { RunDetailPanel } from "./RunDetailPanel";
import { RunsTable, RunsTableSkeleton } from "./RunsTable";
import {
  formatCost,
  formatPercent,
  runTitle,
  sortRuns,
  summarizeRuns,
  type RunSort,
  type RunSortKey,
} from "./runs-util";
import "./runs.css";

export function RunsView({ onOpenTask }: { onOpenTask?: (id: string) => void }) {
  const projectId = useUi((state) => state.projectId);
  const dashboard = useDashboard(projectId);
  const runsQuery = useRuns(projectId);
  const stopProject = useStopProject(projectId);
  const toast = useToast();

  const [sort, setSort] = useState<RunSort>({ key: "started", dir: "desc" });
  const [agentFilter, setAgentFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const runs = useMemo(() => runsQuery.data?.runs ?? [], [runsQuery.data]);
  const degraded = Boolean(runsQuery.data?.degraded);

  const titles = useMemo(() => {
    const map = new Map<string, string>();
    for (const task of dashboard.data?.tasks ?? []) map.set(task.id, task.title);
    for (const run of runs) {
      if (run.task_title && !map.has(run.task_id)) map.set(run.task_id, run.task_title);
    }
    return map;
  }, [dashboard.data, runs]);

  const agents = useMemo(() => {
    const map = new Map<string, string>();
    for (const run of runs) {
      if (run.agent_id) map.set(run.agent_id, run.agent_label || run.agent_id);
    }
    return Array.from(map, ([id, label]) => ({ id, label })).sort((a, b) =>
      a.label.localeCompare(b.label),
    );
  }, [runs]);

  const statuses = useMemo(() => {
    const present = new Set(runs.map((run) => run.status).filter(Boolean));
    const known = Object.keys(RUN_STATUS_LABEL).filter((status) => present.has(status));
    const extra = Array.from(present).filter((status) => !(status in RUN_STATUS_LABEL));
    return [...known, ...extra.sort()];
  }, [runs]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return runs.filter((run) => {
      if (agentFilter !== "all" && run.agent_id !== agentFilter) return false;
      if (statusFilter !== "all" && run.status !== statusFilter) return false;
      if (needle) {
        const title = runTitle(run, titles).toLowerCase();
        if (!title.includes(needle) && !run.task_id.toLowerCase().includes(needle)) return false;
      }
      return true;
    });
  }, [runs, agentFilter, statusFilter, search, titles]);

  const sorted = useMemo(() => sortRuns(filtered, sort), [filtered, sort]);
  const summary = useMemo(() => summarizeRuns(runs), [runs]);
  const hasRunning = summary.running > 0;
  const schedulerRunning = Boolean(dashboard.data?.running) || hasRunning;
  const hasFilters = agentFilter !== "all" || statusFilter !== "all" || search.trim() !== "";
  const selectedRun = runs.find((run) => run.id === selectedId) ?? null;

  const resetFilters = () => {
    setAgentFilter("all");
    setStatusFilter("all");
    setSearch("");
  };

  useEffect(() => {
    setSelectedId(null);
    setSearch("");
    setAgentFilter("all");
    setStatusFilter("all");
    setSort({ key: "started", dir: "desc" });
  }, [projectId]);

  useEffect(() => {
    if (!selectedId || runsQuery.isLoading) return;
    if (!runs.some((run) => run.id === selectedId)) setSelectedId(null);
  }, [selectedId, runs, runsQuery.isLoading]);

  useEffect(() => {
    if (!selectedId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelectedId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId]);

  const handleSort = (key: RunSortKey) => {
    setSort((previous) =>
      previous.key === key
        ? { key, dir: previous.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "desc" },
    );
  };

  const handleStop = async () => {
    try {
      await stopProject.mutateAsync();
      toast.success("已发送停止指令", "正在运行的 Agent 调用会尽快收尾。");
    } catch (error) {
      toast.error("停止失败", (error as Error)?.message);
    }
  };

  const successTone =
    summary.finished === 0
      ? undefined
      : summary.successRate >= 0.8
        ? ("success" as const)
        : summary.successRate < 0.5
          ? ("danger" as const)
          : ("warning" as const);

  return (
    <div className="page wide runs-page">
      <div className="page-head">
        <div className="page-head-text">
          <div className="page-title">
            <h1>运行记录</h1>
            {hasRunning ? (
              <span className="runs-live">
                <span className="runs-live-dot" />
                实时 · 3s
              </span>
            ) : null}
            {degraded ? (
              <span title="运行列表接口不可用，已从任务详情回退读取">
                <Badge tone="warning">回退数据源</Badge>
              </span>
            ) : null}
          </div>
          <div className="page-subtitle">
            每次 Agent 调用的状态、耗时、Token 与原始输出。点任意一行在右侧查看详情，Esc 关闭。
          </div>
        </div>
        <div className="page-actions">
          <Button
            variant="danger"
            onClick={() => void handleStop()}
            loading={stopProject.isPending}
            disabled={!schedulerRunning}
            title={schedulerRunning ? "停止当前项目的调度与运行中的调用" : "当前没有运行中的调用"}
          >
            <Square size={13} />
            停止
          </Button>
          <IconButton
            label="刷新运行记录"
            onClick={() => void runsQuery.refetch()}
            disabled={runsQuery.isFetching}
          >
            <RefreshCw size={14} className={runsQuery.isFetching ? "spin" : undefined} />
          </IconButton>
        </div>
      </div>

      <div className="overview-stats runs-summary">
        <Stat
          label="运行次数"
          value={
            runsQuery.isLoading ? (
              <span className="skeleton runs-sk-value" />
            ) : (
              fmtInt(summary.total)
            )
          }
          hint={summary.running > 0 ? `${summary.running} 条运行中` : "最近 500 次调用"}
        />
        <Stat
          label="成功率"
          value={
            runsQuery.isLoading ? (
              <span className="skeleton runs-sk-value" />
            ) : summary.finished > 0 ? (
              formatPercent(summary.successRate)
            ) : (
              "—"
            )
          }
          hint={
            summary.finished > 0
              ? `${summary.success} 成功 / ${summary.finished} 已完成`
              : "还没有已完成的调用"
          }
          tone={successTone}
        />
        <Stat
          label="总耗时"
          value={
            runsQuery.isLoading ? (
              <span className="skeleton runs-sk-value" />
            ) : (
              fmtDuration(summary.durationMs)
            )
          }
          hint="按已记录的执行时长累计"
        />
        <Stat
          label="总 Token"
          value={
            runsQuery.isLoading ? (
              <span className="skeleton runs-sk-value" />
            ) : (
              fmtInt(summary.tokens)
            )
          }
          hint={`累计成本 ${formatCost(summary.cost)}`}
        />
      </div>

      {runsQuery.isError ? (
        <Panel className="runs-state-card">
          <div className="runs-error-state" role="alert">
            <AlertTriangle size={20} />
            <div className="runs-error-copy">
              <div className="runs-error-title">无法加载运行记录</div>
              <div className="runs-error-hint">{(runsQuery.error as Error)?.message}</div>
              <div className="runs-error-hint">
                已尝试从任务接口回退；如果仍然失败，请确认本地服务正在运行。
              </div>
            </div>
            <Button size="sm" onClick={() => void runsQuery.refetch()}>
              <RefreshCw size={13} />
              重试
            </Button>
          </div>
        </Panel>
      ) : runsQuery.isLoading ? (
        <Panel className="runs-card">
          <RunsTableSkeleton />
        </Panel>
      ) : (
        <>
          <div className="runs-toolbar">
            <div className="runs-search">
              <Search size={13} />
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索任务标题或 ID"
                aria-label="搜索任务标题"
              />
            </div>
            <Select
              className="runs-filter"
              value={agentFilter}
              onChange={(event) => setAgentFilter(event.target.value)}
              aria-label="按 Agent 筛选"
            >
              <option value="all">全部 Agent</option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.label}
                </option>
              ))}
            </Select>
            <Select
              className="runs-filter"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              aria-label="按状态筛选"
            >
              <option value="all">全部状态</option>
              {statuses.map((status) => (
                <option key={status} value={status}>
                  {RUN_STATUS_LABEL[status] ?? status}
                </option>
              ))}
            </Select>
            <span className="runs-count">
              {filtered.length} / {runs.length} 次调用
              {hasFilters ? (
                <button type="button" className="runs-clear-btn" onClick={resetFilters}>
                  清除筛选
                </button>
              ) : null}
            </span>
          </div>

          {runs.length === 0 ? (
            <Panel className="runs-card">
              <EmptyState
                icon={<Activity size={18} />}
                title="还没有运行记录"
                hint="在指挥台启动一次调度或运行一个任务后，这里会按每次 Agent 调用列出状态、耗时与原始输出。"
                action={
                  <Button size="sm" onClick={() => void runsQuery.refetch()}>
                    <RefreshCw size={13} />
                    刷新
                  </Button>
                }
              />
            </Panel>
          ) : (
            <div className={clsx("runs-layout", selectedRun && "has-detail")}>
              <Panel className="runs-card">
                <RunsTable
                  runs={sorted}
                  titles={titles}
                  selectedId={selectedId}
                  onSelect={(id) => setSelectedId((current) => (current === id ? null : id))}
                  sort={sort}
                  onSort={handleSort}
                  onResetFilters={resetFilters}
                />
              </Panel>
              {selectedRun ? (
                <RunDetailPanel
                  key={selectedRun.id}
                  run={selectedRun}
                  taskTitle={runTitle(selectedRun, titles)}
                  onClose={() => setSelectedId(null)}
                  onOpenTask={onOpenTask}
                />
              ) : null}
            </div>
          )}
        </>
      )}
    </div>
  );
}
