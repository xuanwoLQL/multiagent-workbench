import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Clock3,
  MessageSquare,
  RefreshCw,
  Wrench,
} from "lucide-react";
import { api } from "../../api/client";
import type { Run, TaskDetail } from "../../api/types";
import { Badge, Button, EmptyState } from "../../ui/primitives";
import { fmtClock, fmtDateTime, fmtDuration, fmtInt } from "../../lib/format";
import { asArray, normalizeHistory, runDuration, sortRuns, statusLabel } from "./helpers";

interface RunEvent {
  event_id?: number;
  id?: number;
  run_id?: string;
  task_id?: string;
  agent_id?: string;
  kind?: string;
  source_type?: string;
  type?: string;
  text?: string;
  at?: string;
  data?: Record<string, unknown>;
}

interface RunEventResponse {
  items?: RunEvent[];
}

type Category = "status" | "tool" | "output" | "error";
type Filter = "all" | Category;

interface TimelineItem {
  id: string;
  at: string;
  atMs: number;
  category: Category;
  title: string;
  detail?: string;
  agent?: string;
  runId?: string;
  source: "history" | "run" | "event";
}

const KIND_LABEL: Record<string, string> = {
  agent_message_chunk: "Agent 输出",
  agent_thought_chunk: "思考",
  agent_message: "Agent 输出",
  agent_thought: "思考",
  tool_call: "工具调用",
  tool_result: "工具结果",
  tool_output: "工具输出",
  terminal: "终端",
  permission: "权限请求",
  approval: "人工审批",
  error: "错误",
  plan: "计划",
  usage: "用量",
  run_start: "运行开始",
  run_end: "运行结束",
  status: "状态",
  message: "消息",
  task_event: "任务事件",
};

const CATEGORY_LABEL: Record<Category, string> = {
  status: "状态",
  tool: "工具",
  output: "输出",
  error: "错误",
};

function categoryOf(kind: string): Category {
  const value = kind.toLowerCase();
  if (/error|fail|denied|timeout|stuck|exception/.test(value)) return "error";
  if (value.includes("tool") || value.includes("terminal")) return "tool";
  if (
    /status|run[._]?|permission|approval|plan|task[._]?event|usage|quota|start|finish|cancel/.test(
      value,
    )
  ) {
    return "status";
  }
  return "output";
}

function eventText(event: RunEvent): string {
  const data = event.data && typeof event.data === "object" ? event.data : {};
  const candidates = [
    event.text,
    data.text,
    data.message,
    data.summary,
    data.title,
    data.command,
    data.name,
    data.output,
    event.kind,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim().slice(0, 1600);
    }
  }
  const keys = Object.keys(data);
  if (keys.length) {
    try {
      return JSON.stringify(data, null, 2).slice(0, 1600);
    } catch {
      return keys.join(", ");
    }
  }
  return "";
}

function eventKind(event: RunEvent): string {
  return String(event.kind || event.source_type || event.type || "event");
}

function CategoryIcon({ category }: { category: Category }) {
  if (category === "error") return <AlertTriangle size={12} />;
  if (category === "tool") return <Wrench size={12} />;
  if (category === "status") return <Activity size={12} />;
  return <MessageSquare size={12} />;
}

function dayLabel(iso: string): string {
  if (!iso) return "未知时间";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function timeOf(iso: string | undefined): number {
  if (!iso) return 0;
  const value = new Date(iso).getTime();
  return Number.isNaN(value) ? 0 : value;
}

export function ActivityTab({
  detail,
  taskId,
  projectId,
  onOpenLogs,
}: {
  detail: TaskDetail;
  taskId: string;
  projectId: string;
  onOpenLogs: (runId?: string) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");

  const eventsQuery = useQuery({
    queryKey: ["task-run-events", projectId, taskId],
    queryFn: async () => {
      const response = await api.get<RunEventResponse>(
        `/api/projects/${projectId}/run-events?limit=600`,
      );
      return Array.isArray(response.items) ? response.items : [];
    },
    enabled: Boolean(projectId),
    refetchInterval: 4000,
    refetchOnWindowFocus: true,
  });

  // The contract exposes /api/tasks/{tid}/runs; older servers only return runs
  // inside the task detail payload. Try once, then stop polling on a 404.
  const runsQuery = useQuery({
    queryKey: ["task-runs-poll", taskId],
    queryFn: async (): Promise<Run[] | null> => {
      if (projectId) {
        try {
          const list = await api.get<Run[]>(
            `/api/projects/${projectId}/runs?limit=200`,
          );
          return list.filter((run) => run.task_id === taskId);
        } catch {
          return null;
        }
      }
      try {
        return await api.get<Run[]>(`/api/tasks/${taskId}/runs`);
      } catch {
        return null;
      }
    },
    enabled: Boolean(taskId),
    refetchInterval: (query) => (query.state.data === null ? false : 6000),
    retry: false,
  });

  const runs = useMemo(() => {
    const live = runsQuery.data;
    return sortRuns((live && live.length ? live : asArray(detail.runs)) as Run[]);
  }, [runsQuery.data, detail.runs]);

  const items = useMemo<TimelineItem[]>(() => {
    const output: TimelineItem[] = [];
    for (const entry of normalizeHistory(detail.history)) {
      const at = String(entry.at ?? "");
      output.push({
        id: `history-${at}-${entry.from ?? ""}-${entry.status}`,
        at,
        atMs: timeOf(at),
        category: "status",
        title: `状态变更 · ${entry.from ? `${statusLabel(entry.from)} → ` : ""}${statusLabel(
          entry.status,
        )}`,
        detail: entry.reason || undefined,
        source: "history",
      });
    }
    for (const run of runs) {
      const agent = run.agent_label || run.agent_id || "agent";
      const at = run.started_at || "";
      output.push({
        id: `run-start-${run.id}`,
        at,
        atMs: timeOf(at),
        category: "status",
        title: `${agent} 开始第 ${run.attempt || 1} 次尝试`,
        detail: [run.protocol, run.role].filter(Boolean).join(" · ") || undefined,
        agent,
        runId: run.id,
        source: "run",
      });
      if (run.finished_at) {
        const detailParts = [
          runDuration(run) ? fmtDuration(runDuration(run)) : "",
          run.exit_code !== null && run.exit_code !== undefined ? `exit ${run.exit_code}` : "",
          run.cost_tokens ? `${fmtInt(run.cost_tokens)} tokens` : "",
        ].filter(Boolean);
        output.push({
          id: `run-end-${run.id}`,
          at: run.finished_at,
          atMs: timeOf(run.finished_at),
          category:
            run.status === "failed" || run.status === "interrupted" ? "error" : "status",
          title: `${agent} · ${statusLabel(run.status)}`,
          detail: [detailParts.join(" · "), run.error || ""].filter(Boolean).join("\n") || undefined,
          agent,
          runId: run.id,
          source: "run",
        });
      }
    }
    const events = eventsQuery.data ?? [];
    for (const event of events) {
      if (event.task_id && String(event.task_id) !== taskId) continue;
      if (!event.task_id) continue;
      const kind = eventKind(event);
      const at = String(event.at ?? "");
      output.push({
        id: `event-${event.event_id ?? event.id ?? `${at}-${kind}`}`,
        at,
        atMs: timeOf(at),
        category: categoryOf(kind),
        title: KIND_LABEL[kind] ?? kind,
        detail: eventText(event) || undefined,
        agent: event.agent_id ? String(event.agent_id) : undefined,
        runId: event.run_id ? String(event.run_id) : undefined,
        source: "event",
      });
    }
    const deduped = new Map<string, TimelineItem>();
    for (const item of output) deduped.set(item.id, item);
    return [...deduped.values()].sort((left, right) => {
      if (left.atMs === right.atMs) return right.id.localeCompare(left.id);
      return right.atMs - left.atMs;
    });
  }, [detail.history, runs, eventsQuery.data, taskId]);

  const counts = useMemo(() => {
    const base: Record<Filter, number> = { all: items.length, status: 0, tool: 0, output: 0, error: 0 };
    for (const item of items) base[item.category] += 1;
    return base;
  }, [items]);

  const visible = filter === "all" ? items : items.filter((item) => item.category === filter);
  const groups = useMemo(() => {
    const result: Array<{ label: string; items: TimelineItem[] }> = [];
    for (const item of visible.slice(0, 500)) {
      const label = dayLabel(item.at);
      const last = result[result.length - 1];
      if (last && last.label === label) last.items.push(item);
      else result.push({ label, items: [item] });
    }
    return result;
  }, [visible]);

  const refresh = () => {
    void eventsQuery.refetch();
    void runsQuery.refetch();
  };

  const filters: Array<{ value: Filter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "status", label: "状态" },
    { value: "tool", label: "工具" },
    { value: "output", label: "输出" },
    { value: "error", label: "错误" },
  ];

  return (
    <div className="td-activity">
      <div className="td-activity-head">
        <div>
          <div className="td-activity-title">
            <Activity size={15} /> 实时活动
            {detail.task.status === "running" ? (
              <span className="td-live-pill">
                <span className="td-live-dot" /> LIVE
              </span>
            ) : null}
          </div>
          <div className="td-activity-sub">
            状态变化、Agent 调用与工具事件，按时间倒序排列
          </div>
        </div>
        <div className="td-activity-actions">
          <span className="td-refresh-note">
            {eventsQuery.isFetching ? <RefreshCw size={12} className="spin" /> : <Clock3 size={12} />}
            每 4 秒刷新
          </span>
          <Button size="sm" variant="ghost" onClick={refresh}>
            <RefreshCw size={13} /> 刷新
          </Button>
        </div>
      </div>

      <div className="td-filter-bar" role="tablist" aria-label="活动筛选">
        {filters.map((item) => (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={filter === item.value}
            className={clsx("td-filter", filter === item.value && "is-active")}
            onClick={() => setFilter(item.value)}
          >
            {item.label}
            <span>{counts[item.value]}</span>
          </button>
        ))}
      </div>

      {groups.length ? (
        <div className="td-timeline">
          {groups.map((group) => (
            <section key={group.label} className="td-timeline-group">
              <div className="td-timeline-day">{group.label}</div>
              <ol className="td-timeline-list">
                {group.items.map((item) => (
                  <li
                    key={item.id}
                    className={clsx("td-timeline-item", `is-${item.category}`)}
                  >
                    <span className="td-timeline-rail">
                      <span className="td-timeline-dot">
                        <CategoryIcon category={item.category} />
                      </span>
                    </span>
                    <div className="td-timeline-card">
                      <div className="td-timeline-top">
                        <span className="td-timeline-title">{item.title}</span>
                        <span className="td-timeline-time" title={fmtDateTime(item.at)}>
                          {item.at ? fmtClock(item.at) : "—"}
                        </span>
                      </div>
                      <div className="td-timeline-meta">
                        <Badge tone="outline">{CATEGORY_LABEL[item.category]}</Badge>
                        {item.agent ? <span className="td-timeline-agent">{item.agent}</span> : null}
                        {item.source === "history" ? <span>任务状态</span> : null}
                        {item.runId ? (
                          <button
                            type="button"
                            className="td-link td-timeline-log"
                            onClick={() => onOpenLogs(item.runId)}
                          >
                            日志 <ArrowUpRight size={11} />
                          </button>
                        ) : null}
                      </div>
                      {item.detail ? (
                        <div className="td-timeline-text" title={item.detail}>
                          {item.detail}
                        </div>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<Activity size={20} />}
          title={filter === "all" ? "还没有活动记录" : "这个筛选下没有事件"}
          hint={
            filter === "all"
              ? "运行任务后，Agent 的状态、工具调用和输出会实时出现在这里。"
              : "切换到「全部」查看其他类型的事件。"
          }
          action={
            filter !== "all" ? (
              <Button size="sm" onClick={() => setFilter("all")}>
                查看全部
              </Button>
            ) : undefined
          }
        />
      )}
    </div>
  );
}
