import clsx from "clsx";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { RunStatusBadge } from "../../components/StatusBadge";
import { agentColor, fmtDateTime, fmtDuration, fmtInt, initials } from "../../lib/format";
import type { RunRecord } from "./runs-api";
import {
  formatCost,
  runDurationMs,
  runTitle,
  shortId,
  useNow,
  type RunSort,
  type RunSortKey,
} from "./runs-util";

interface RunsTableProps {
  runs: RunRecord[];
  titles: Map<string, string>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  sort: RunSort;
  onSort: (key: RunSortKey) => void;
  onResetFilters: () => void;
}

function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
  align,
}: {
  label: string;
  sortKey: RunSortKey;
  sort: RunSort;
  onSort: (key: RunSortKey) => void;
  align?: "right";
}) {
  const active = sort.key === sortKey;
  return (
    <th
      scope="col"
      className={clsx("runs-th", align === "right" && "runs-th-num")}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        className={clsx("runs-sort", active && "is-active")}
        onClick={() => onSort(sortKey)}
        title={`按${label}排序`}
      >
        {label}
        {active ? (
          sort.dir === "asc" ? (
            <ArrowUp size={12} />
          ) : (
            <ArrowDown size={12} />
          )
        ) : (
          <ChevronsUpDown size={12} className="runs-sort-idle" />
        )}
      </button>
    </th>
  );
}

export function RunsTable({
  runs,
  titles,
  selectedId,
  onSelect,
  sort,
  onSort,
  onResetFilters,
}: RunsTableProps) {
  const now = useNow(runs.some((run) => run.status === "running"));

  const durationOf = (run: RunRecord): number => {
    if (run.status === "running") {
      const started = run.started_at ? new Date(run.started_at).getTime() : NaN;
      if (!Number.isNaN(started)) return Math.max(runDurationMs(run), now - started);
    }
    return runDurationMs(run);
  };

  return (
    <div className="runs-table-wrap">
      <table className="runs-table">
        <thead>
          <tr>
            <th scope="col" className="runs-th">
              状态
            </th>
            <th scope="col" className="runs-th">
              Agent
            </th>
            <th scope="col" className="runs-th">
              任务
            </th>
            <th scope="col" className="runs-th">
              尝试
            </th>
            <SortHeader label="耗时" sortKey="duration" sort={sort} onSort={onSort} align="right" />
            <th scope="col" className="runs-th runs-th-num">
              Token
            </th>
            <th scope="col" className="runs-th runs-th-num">
              成本
            </th>
            <SortHeader label="开始" sortKey="started" sort={sort} onSort={onSort} />
            <th scope="col" className="runs-th runs-th-num">
              退出码
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.length === 0 ? (
            <tr>
              <td colSpan={9} className="runs-empty-cell">
                <div className="runs-empty-title">没有符合条件的调用</div>
                <div className="runs-empty-hint">换个 Agent、状态或关键词再试。</div>
                <button type="button" className="runs-clear-btn" onClick={onResetFilters}>
                  清除筛选
                </button>
              </td>
            </tr>
          ) : (
            runs.map((run) => {
              const agent = run.agent_label || run.agent_id || "auto";
              const title = runTitle(run, titles);
              const selected = selectedId === run.id;
              return (
                <tr
                  key={run.id}
                  className={clsx("runs-row", selected && "is-selected")}
                  tabIndex={0}
                  aria-selected={selected}
                  onClick={() => onSelect(run.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(run.id);
                    }
                  }}
                >
                  <td>
                    <RunStatusBadge status={run.status} />
                  </td>
                  <td>
                    <span className="runs-agent">
                      <span
                        className="runs-agent-avatar"
                        style={{ background: `${agentColor(run.agent_id)}1a`, color: agentColor(run.agent_id) }}
                      >
                        {initials(agent)}
                      </span>
                      <span className="runs-agent-name" title={`${agent} · ${run.agent_id}`}>
                        {agent}
                      </span>
                    </span>
                  </td>
                  <td className="runs-task-cell">
                    <div className="runs-task-title" title={title}>
                      {title}
                    </div>
                    <div className="runs-task-sub mono">{shortId(run.task_id, 10)}</div>
                  </td>
                  <td className="runs-attempt">#{run.attempt}</td>
                  <td className="runs-num">
                    {run.status === "running" ? (
                      <span className="runs-duration-live">
                        <span className="runs-live-dot" />
                        {fmtDuration(durationOf(run))}
                      </span>
                    ) : (
                      fmtDuration(durationOf(run))
                    )}
                  </td>
                  <td className="runs-num">{fmtInt(run.cost_tokens)}</td>
                  <td className="runs-num">{formatCost(run.cost_amount)}</td>
                  <td className="runs-started">{fmtDateTime(run.started_at)}</td>
                  <td className="runs-num">
                    {run.exit_code === null || run.exit_code === undefined ? (
                      <span className="muted">—</span>
                    ) : (
                      <span
                        className={clsx("runs-exit mono", run.exit_code !== 0 && "is-bad")}
                        title={run.exit_code !== 0 ? "非零退出码" : "正常退出"}
                      >
                        {run.exit_code}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}

export function RunsTableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="runs-skeleton" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div className="runs-skeleton-row" key={index}>
          <span className="skeleton runs-sk" style={{ width: 62 }} />
          <span className="skeleton runs-sk" style={{ width: 88 }} />
          <span className="skeleton runs-sk" style={{ flex: 1, minWidth: 120 }} />
          <span className="skeleton runs-sk" style={{ width: 48 }} />
          <span className="skeleton runs-sk" style={{ width: 62 }} />
          <span className="skeleton runs-sk" style={{ width: 70 }} />
          <span className="skeleton runs-sk" style={{ width: 48 }} />
        </div>
      ))}
    </div>
  );
}
