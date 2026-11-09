import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  Copy,
  Download,
  ExternalLink,
  FileText,
  GitCompare,
  Info,
  Layers,
  Loader2,
  RefreshCw,
  Search,
  Terminal,
  Timer,
  Users,
  X,
} from "lucide-react";
import { ApiError, api, qk } from "../../api/client";
import type { AgentUsage, Run, Task } from "../../api/types";
import { useAgents } from "../../app/queries";
import { RunStatusBadge } from "../../components/StatusBadge";
import {
  Badge,
  Button,
  EmptyState,
  IconButton,
  Panel,
  SectionHeader,
  Segmented,
  Spinner,
} from "../../ui/primitives";
import { ConfirmDialog, Dialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import {
  agentColor,
  fmtBytes,
  fmtDateTime,
  fmtDuration,
  fmtInt,
  initials,
  stripAnsi,
} from "../../lib/format";
import {
  copyText,
  downloadText,
  openLocalPath,
  sortRuns,
  statusLabel,
  useNow,
} from "./helpers";
import "./compare.css";

/* ------------------------------------------------------------------ shapes */

interface CompareArtifact {
  sha256?: string | null;
  size?: number | null;
  abs_path?: string | null;
  diff?: string;
  diff_text?: string;
}

interface CompareFile {
  rel_path: string;
  identical?: boolean;
  per_run?: Record<string, CompareArtifact>;
  diff?: string;
  diff_text?: string;
}

interface CompareRunRef {
  run_id: string;
  agent_id: string;
  status: string;
  started_at: string;
}

interface CompareResponse {
  task_id: string;
  runs?: CompareRunRef[];
  files?: CompareFile[];
}

interface FanoutResult {
  agent_id?: string;
  run_id?: string;
  status?: string;
  acceptance?: Record<string, unknown>;
  changes?: string[];
  error?: string | null;
}

interface FanoutResponse {
  task_id?: string;
  results?: FanoutResult[];
  best_agent?: string | null;
}

interface AgentDetail extends AgentUsage {
  command?: string | null;
  error?: string;
}

interface DisplayRun {
  run_id: string;
  agent_id: string;
  agent_label?: string;
  protocol?: string;
  role?: string;
  attempt?: number;
  status: string;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number;
  cost_tokens?: number;
  cost_amount?: number;
  exit_code?: number | null;
  error?: string | null;
}

interface RunOutput {
  run?: {
    id?: string;
    agent_id?: string;
    status?: string;
    exit_code?: number | null;
    duration_ms?: number;
    cost_tokens?: number;
    started_at?: string;
    error?: string | null;
  };
  prompt?: string;
  stdout?: string;
  stderr?: string;
}

type ColumnMode = "latest" | "all";
type OutputChannel = "prompt" | "stdout" | "stderr";
type VerdictKind = "same" | "diff" | "partial" | "none";

interface FileVerdict {
  kind: VerdictKind;
  label: string;
  tone: "default" | "success" | "warning" | "danger";
}

/* ------------------------------------------------------------------ helpers */

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error ?? "未知错误");
}

function startedMs(run: { started_at?: string | null }): number {
  const value = run.started_at ? new Date(run.started_at).getTime() : NaN;
  return Number.isNaN(value) ? 0 : value;
}

function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value === 0) return "¥0.00";
  if (value < 0.01) return `¥${value.toFixed(4)}`;
  return `¥${value.toFixed(2)}`;
}

function shortHash(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length > length ? value.slice(0, length) : value;
}

function shortLabel(value: string): string {
  const text = String(value ?? "agent");
  return text.length > 22 ? `${text.slice(0, 21)}…` : text;
}

function fileDiffText(file: CompareFile): string {
  if (file.diff) return file.diff;
  if (file.diff_text) return file.diff_text;
  for (const artifact of Object.values(file.per_run ?? {})) {
    if (artifact?.diff) return artifact.diff;
    if (artifact?.diff_text) return artifact.diff_text;
  }
  return "";
}

function fileVerdict(
  file: CompareFile,
  columns: DisplayRun[],
  totalRuns = 0,
): FileVerdict {
  if (!columns.length) return { kind: "none", label: "无列", tone: "default" };
  if (columns.length < 2) return { kind: "none", label: "单列", tone: "default" };
  const present = columns
    .map((run) => ({ run, artifact: file.per_run?.[run.run_id] }))
    .filter((item) => Boolean(item.artifact));
  if (!present.length) {
    const covered = Object.keys(file.per_run ?? {}).length;
    return covered && totalRuns
      ? { kind: "none", label: "仅历史尝试", tone: "default" }
      : { kind: "none", label: "无产出", tone: "default" };
  }
  const hashes = new Set(
    present.map((item) => item.artifact?.sha256).filter((value): value is string => Boolean(value)),
  );
  if (hashes.size > 1) {
    return { kind: "diff", label: "不同", tone: "danger" };
  }
  if (present.length < columns.length) {
    return {
      kind: "partial",
      label: `仅 ${present.length}/${columns.length} 列`,
      tone: "warning",
    };
  }
  if (!hashes.size) {
    const sizes = new Set(present.map((item) => Number(item.artifact?.size ?? -1)));
    return sizes.size > 1
      ? { kind: "diff", label: "大小不同", tone: "danger" }
      : { kind: "none", label: "无哈希", tone: "default" };
  }
  return { kind: "same", label: "一致", tone: "success" };
}

function mergeRuns(refs: CompareRunRef[] | undefined, detailRuns: Run[]): DisplayRun[] {
  const meta = new Map(detailRuns.map((run) => [run.id, run]));
  if (refs && refs.length) {
    return refs.map((ref) => {
      const source = meta.get(ref.run_id);
      return {
        run_id: ref.run_id,
        agent_id: ref.agent_id || source?.agent_id || "agent",
        agent_label: source?.agent_label,
        protocol: source?.protocol,
        role: source?.role,
        attempt: source?.attempt,
        status: ref.status || source?.status || "unknown",
        started_at: ref.started_at || source?.started_at || "",
        finished_at: source?.finished_at ?? null,
        duration_ms: source?.duration_ms,
        cost_tokens: source?.cost_tokens,
        cost_amount: source?.cost_amount,
        exit_code: source?.exit_code ?? null,
        error: source?.error ?? null,
      };
    });
  }
  return detailRuns.map((run) => ({
    run_id: run.id,
    agent_id: run.agent_id || "agent",
    agent_label: run.agent_label,
    protocol: run.protocol,
    role: run.role,
    attempt: run.attempt,
    status: run.status,
    started_at: run.started_at,
    finished_at: run.finished_at,
    duration_ms: run.duration_ms,
    cost_tokens: run.cost_tokens,
    cost_amount: run.cost_amount,
    exit_code: run.exit_code,
    error: run.error,
  }));
}

function liveDuration(run: DisplayRun, now: number): number {
  if (run.status === "running") {
    return Math.max(0, now - startedMs(run));
  }
  const recorded = Number(run.duration_ms) || 0;
  if (recorded) return recorded;
  const start = startedMs(run);
  const finished = run.finished_at ? new Date(run.finished_at).getTime() : NaN;
  if (!start || Number.isNaN(finished) || finished < start) return 0;
  return finished - start;
}

function statusTone(status: string): "success" | "danger" | "warning" | "accent" | "default" {
  if (status === "success") return "success";
  if (status === "failed" || status === "interrupted") return "danger";
  if (status === "blocked" || status === "waiting") return "warning";
  if (status === "running") return "accent";
  return "default";
}

/* ------------------------------------------------------------------ pieces */

function AgentAvatar({ id, label }: { id: string; label: string }) {
  return (
    <span
      className="td-avatar"
      style={{ background: `${agentColor(id)}1a`, color: agentColor(id) }}
      aria-hidden="true"
    >
      {initials(label || id)}
    </span>
  );
}

function DiffText({ text }: { text: string }) {
  const lines = text.split(/\r?\n/);
  return (
    <pre className="td-cmp-diff" aria-label="差异文本">
      {lines.map((line, index) => {
        const kind =
          line.startsWith("+++") || line.startsWith("---")
            ? "meta"
            : line.startsWith("@@")
              ? "hunk"
              : line.startsWith("+")
                ? "add"
                : line.startsWith("-")
                  ? "del"
                  : "context";
        return (
          <span key={`${index}-${line}`} className={clsx("td-cmp-diff-line", `is-${kind}`)}>
            {line || " "}
          </span>
        );
      })}
    </pre>
  );
}

function OutputPane({
  run,
  now,
  onOpen,
}: {
  run: DisplayRun;
  now: number;
  onOpen: () => void;
}) {
  const query = useQuery({
    queryKey: ["compare-run-output", run.run_id],
    queryFn: () => api.get<RunOutput>(`/api/runs/${run.run_id}/output`),
    staleTime: 15000,
    retry: 1,
  });
  const stdout = stripAnsi(query.data?.stdout ?? "");
  const stderr = stripAnsi(query.data?.stderr ?? "");
  const text = stdout.trim() ? stdout : stderr;
  const preview = text.length > 5000 ? `…\n${text.slice(-5000)}` : text;
  const label = run.agent_label || run.agent_id;

  return (
    <div className="td-cmp-output-pane">
      <div className="td-cmp-output-head">
        <AgentAvatar id={run.agent_id} label={label} />
        <div className="td-cmp-output-heading">
          <span className="td-cmp-output-name">{shortLabel(label)}</span>
          <span className="td-cmp-output-meta">
            #{run.attempt || 1} · {fmtDuration(liveDuration(run, now))}
          </span>
        </div>
        <RunStatusBadge status={run.status} />
        <IconButton label="查看完整输出" onClick={onOpen}>
          <ExternalLink size={13} />
        </IconButton>
      </div>
      {query.isPending ? (
        <div className="td-cmp-output-loading">
          <Spinner size={13} /> 读取输出…
        </div>
      ) : query.isError ? (
        <div className="td-cmp-output-error">{errorText(query.error)}</div>
      ) : (
        <pre className="td-cmp-output-pre">{preview.trim() ? preview : "(无输出)"}</pre>
      )}
      {text.length > 5000 ? (
        <div className="td-cmp-output-more">仅显示尾部 5000 字符</div>
      ) : null}
    </div>
  );
}

function RunOutputDialog({
  run,
  onClose,
}: {
  run: DisplayRun;
  onClose: () => void;
}) {
  const toast = useToast();
  const [channel, setChannel] = useState<OutputChannel>("stdout");
  const query = useQuery({
    queryKey: ["compare-run-output", run.run_id],
    queryFn: () => api.get<RunOutput>(`/api/runs/${run.run_id}/output`),
    staleTime: 15000,
    retry: 1,
  });
  const label = run.agent_label || run.agent_id;
  const text =
    channel === "prompt"
      ? query.data?.prompt ?? ""
      : channel === "stderr"
        ? query.data?.stderr ?? ""
        : query.data?.stdout ?? "";
  const clean = stripAnsi(text);

  const channels: Array<{ key: OutputChannel; label: string; value: string }> = [
    { key: "prompt", label: "Prompt", value: query.data?.prompt ?? "" },
    { key: "stdout", label: "stdout", value: query.data?.stdout ?? "" },
    { key: "stderr", label: "stderr", value: query.data?.stderr ?? "" },
  ];

  return (
    <Dialog
      open
      onClose={onClose}
      width={900}
      title={`${shortLabel(label)} · 运行输出`}
      description={
        <span>
          #{run.attempt || 1} · {statusLabel(run.status)} · {fmtDuration(liveDuration(run, Date.now()))}
          {run.exit_code !== null && run.exit_code !== undefined ? ` · exit ${run.exit_code}` : ""}
        </span>
      }
      footer={
        <>
          <Button
            size="sm"
            variant="ghost"
            disabled={!clean}
            onClick={async () => {
              const ok = await copyText(clean);
              if (ok) toast.success("已复制");
              else toast.error("复制失败");
            }}
          >
            <Copy size={12} /> 复制
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={!clean}
            onClick={() => downloadText(`${run.agent_id}-${channel}.txt`, clean)}
          >
            <Download size={12} /> 下载
          </Button>
          <Button onClick={onClose}>关闭</Button>
        </>
      }
    >
      <div className="td-cmp-out-tabs" role="tablist" aria-label="运行输出通道">
        {channels.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={channel === item.key}
            className={clsx("td-cmp-out-tab", channel === item.key && "is-active")}
            onClick={() => setChannel(item.key)}
          >
            {item.label}
            <span className="td-cmp-out-count">{fmtInt(item.value ? item.value.length : 0)}</span>
          </button>
        ))}
      </div>
      {query.isPending ? (
        <div className="td-cmp-output-loading">
          <Spinner size={13} /> 读取输出…
        </div>
      ) : query.isError ? (
        <div className="td-cmp-out-error">
          <AlertTriangle size={14} /> {errorText(query.error)}
        </div>
      ) : (
        <pre className="td-cmp-out-full">{clean || "(空)"}</pre>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ main */

export function CompareTab({
  taskId,
  task,
  runs,
  onChanged,
}: {
  taskId: string;
  task: Task;
  runs: Run[];
  onChanged?: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const agentsQuery = useAgents();
  const [fanoutOpen, setFanoutOpen] = useState(false);
  const [confirmFanout, setConfirmFanout] = useState(false);
  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
  const [fanoutStartedAt, setFanoutStartedAt] = useState<number | null>(null);
  const [columnMode, setColumnMode] = useState<ColumnMode>("latest");
  const [expandedPath, setExpandedPath] = useState<string | null>(null);
  const [fileQuery, setFileQuery] = useState("");
  const [onlyDiffs, setOnlyDiffs] = useState(false);
  const [outputRunId, setOutputRunId] = useState<string | null>(null);

  const compareQuery = useQuery({
    queryKey: ["task-compare", taskId],
    queryFn: () => api.get<CompareResponse>(`/api/tasks/${taskId}/compare`),
    retry: 1,
    refetchInterval: (query) => {
      const refs = query.state.data?.runs ?? [];
      const fanoutRunning = Boolean(fanoutStartedAt);
      return fanoutRunning || refs.some((run) => run.status === "running") ? 3000 : 20000;
    },
  });

  const fanout = useMutation({
    mutationFn: (agentIds: string[]) =>
      api.post<FanoutResponse>(`/api/tasks/${taskId}/fanout`, { agents: agentIds }),
    onMutate: () => {
      setFanoutStartedAt(Date.now());
    },
    onSuccess: (data) => {
      const results = data.results ?? [];
      const successes = results.filter((item) => item.status === "success");
      const best = data.best_agent
        ? agentsQuery.data?.find((agent) => (agent.id || agent.agent_id) === data.best_agent)?.label ||
          data.best_agent
        : "";
      if (successes.length) {
        toast.success(
          `扇出完成：${successes.length}/${results.length} 个 Agent 成功`,
          best ? `最佳结果：${best}` : undefined,
        );
      } else if (results.length) {
        toast.error("扇出完成，但没有 Agent 通过验收", `${results.length} 个运行均已记录`);
      } else {
        toast.info("扇出接口已返回", "没有产生新的运行记录");
      }
      void client.invalidateQueries({ queryKey: ["task-compare", taskId] });
      void client.invalidateQueries({ queryKey: qk.task(taskId) });
      onChanged?.();
    },
    onError: (error) => {
      toast.error("扇出失败", errorText(error));
      void client.invalidateQueries({ queryKey: ["task-compare", taskId] });
      void client.invalidateQueries({ queryKey: qk.task(taskId) });
      onChanged?.();
    },
    onSettled: () => {
      setFanoutStartedAt(null);
    },
  });

  const hasRunning =
    Boolean(fanoutStartedAt) ||
    runs.some((run) => run.status === "running") ||
    (compareQuery.data?.runs ?? []).some((run) => run.status === "running");
  const now = useNow(hasRunning);

  const agents = (agentsQuery.data ?? []) as AgentDetail[];
  const readyAgents = useMemo(() => {
    return agents
      .filter((agent) => {
        if (agent.enabled === false) return false;
        if (agent.error) return false;
        if (agent.command === null) return false;
        return Boolean(agent.id || agent.agent_id);
      })
      .sort((left, right) => {
        const leftLabel = left.label || left.id || left.agent_id || "";
        const rightLabel = right.label || right.id || right.agent_id || "";
        return leftLabel.localeCompare(rightLabel);
      });
  }, [agents]);
  const readyIds = useMemo(
    () => new Set(readyAgents.map((agent) => String(agent.id || agent.agent_id))),
    [readyAgents],
  );

  const agentLabel = (id: string): string => {
    const detail = agents.find((agent) => (agent.id || agent.agent_id) === id);
    return detail?.label || runs.find((run) => run.agent_id === id)?.agent_label || id;
  };

  const displayRuns = useMemo(() => {
    if (compareQuery.data) {
      const merged = mergeRuns(compareQuery.data.runs, runs);
      if (merged.length || !runs.length) return merged;
    }
    return mergeRuns(undefined, sortRuns(runs));
  }, [compareQuery.data, runs]);

  const groups = useMemo(() => {
    const map = new Map<string, DisplayRun[]>();
    for (const run of displayRuns) {
      const list = map.get(run.agent_id) ?? [];
      list.push(run);
      map.set(run.agent_id, list);
    }
    return Array.from(map.entries())
      .map(([agentId, list]) => ({
        agentId,
        label: list.find((run) => run.agent_label)?.agent_label || agentLabel(agentId),
        protocol: list.find((run) => run.protocol)?.protocol,
        runs: [...list].sort((left, right) => {
          const attempt = (left.attempt || 1) - (right.attempt || 1);
          return attempt || startedMs(left) - startedMs(right);
        }),
        latest: Math.max(...list.map(startedMs)),
      }))
      .sort((left, right) => right.latest - left.latest);
  }, [displayRuns, agents, runs]);

  const latestPerAgent = useMemo(() => {
    const map = new Map<string, DisplayRun>();
    for (const run of displayRuns) {
      const previous = map.get(run.agent_id);
      const rank = (item: DisplayRun) => (item.status === "success" ? 2 : 1);
      if (
        !previous ||
        rank(run) > rank(previous) ||
        (rank(run) === rank(previous) && startedMs(run) >= startedMs(previous))
      ) {
        map.set(run.agent_id, run);
      }
    }
    return Array.from(map.values()).sort((left, right) => startedMs(left) - startedMs(right));
  }, [displayRuns]);

  const columns = columnMode === "latest" ? latestPerAgent : displayRuns;
  const previewColumns = columns.slice(0, 4);
  const files = compareQuery.data?.files ?? [];

  const rows = useMemo(() => {
    const query = fileQuery.trim().toLowerCase();
    return files
      .map((file) => ({
        file,
        verdict: fileVerdict(file, columns, displayRuns.length),
      }))
      .filter(({ file, verdict }) => {
        if (query && !file.rel_path.toLowerCase().includes(query)) return false;
        if (onlyDiffs && verdict.kind !== "diff" && verdict.kind !== "partial") return false;
        return true;
      })
      .sort((left, right) => {
        const rank = (kind: VerdictKind) =>
          kind === "diff" ? 0 : kind === "partial" ? 1 : kind === "same" ? 2 : 3;
        const delta = rank(left.verdict.kind) - rank(right.verdict.kind);
        return delta || left.file.rel_path.localeCompare(right.file.rel_path);
      });
  }, [files, columns, fileQuery, onlyDiffs]);

  const selectedFile = useMemo(
    () => files.find((file) => file.rel_path === expandedPath) ?? null,
    [files, expandedPath],
  );
  const selectedFileDiff = selectedFile ? fileDiffText(selectedFile) : "";

  const successCount = displayRuns.filter((run) => run.status === "success").length;
  const diffCount = files.filter(
    (file) => fileVerdict(file, columns, displayRuns.length).kind === "diff",
  ).length;
  const runningCount = displayRuns.filter((run) => run.status === "running").length;
  const selectedIds = selectedAgents.filter((id) => readyIds.has(id));
  const fanoutDisabledReason =
    task.status === "running"
      ? "任务正在执行，结束后才能扇出。"
      : agentsQuery.isSuccess && readyAgents.length < 2
        ? "至少需要 2 个可用 Agent 才能扇出。"
        : undefined;
  const elapsed = fanoutStartedAt ? Math.max(0, now - fanoutStartedAt) : 0;
  const outputRun = outputRunId
    ? displayRuns.find((run) => run.run_id === outputRunId) ?? null
    : null;

  const openArtifact = async (path?: string | null) => {
    if (!path) return;
    const result = await openLocalPath(path);
    if (result === "desktop") toast.success("已打开产物");
    else if (result === "copied") toast.info("已复制产物路径", path);
    else toast.error("无法打开产物", path);
  };

  const toggleAgent = (id: string) => {
    setSelectedAgents((previous) => {
      const active = previous.filter((item) => readyIds.has(item));
      if (active.includes(id)) return active.filter((item) => item !== id);
      if (active.length >= 4) return active;
      return [...active, id];
    });
  };

  return (
    <div className="td-cmp">
      <Panel className="td-panel td-cmp-head">
        <SectionHeader
          title={
            <span className="td-section-title">
              <GitCompare size={15} /> Agent 对比
            </span>
          }
          subtitle="同一任务在不同 Agent / 尝试之间的运行、产物与输出差异。"
          actions={
            <>
              {runningCount ? (
                <Badge tone="accent" dot>
                  实时刷新
                </Badge>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void compareQuery.refetch()}
              >
                <RefreshCw
                  size={12}
                  className={compareQuery.isFetching ? "spin" : undefined}
                />{" "}
                刷新
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={() => setFanoutOpen(true)}
                disabled={Boolean(fanoutDisabledReason) || fanout.isPending}
                title={fanoutDisabledReason}
              >
                <GitCompare size={13} /> 扇出对比
              </Button>
            </>
          }
        />
        {fanoutDisabledReason ? (
          <div className="td-cmp-note">
            <Info size={13} /> {fanoutDisabledReason}
          </div>
        ) : null}
      </Panel>

      <div className="td-stat-strip td-cmp-stats">
        <div className="stat">
          <div className="stat-label">运行</div>
          <div className="stat-value">{fmtInt(displayRuns.length)}</div>
          <div className="stat-hint">{groups.length} 个 Agent</div>
        </div>
        <div className="stat">
          <div className="stat-label">成功</div>
          <div className={clsx("stat-value", successCount ? "is-success" : undefined)}>
            {fmtInt(successCount)}
          </div>
          <div className="stat-hint">{runningCount ? `${runningCount} 个运行中` : "按运行状态统计"}</div>
        </div>
        <div className="stat">
          <div className="stat-label">产物文件</div>
          <div className="stat-value">{fmtInt(files.length)}</div>
          <div className="stat-hint">{columns.length} 列参与对比</div>
        </div>
        <div className="stat">
          <div className="stat-label">存在差异</div>
          <div className={clsx("stat-value", diffCount ? "is-danger" : undefined)}>
            {fmtInt(diffCount)}
          </div>
          <div className="stat-hint">
            {diffCount
              ? "需要人工判断"
              : columns.length < 2 && displayRuns.length > 1
                ? "当前只有一列，切换全部尝试"
                : displayRuns.length > 1
                  ? "当前列没有哈希差异"
                  : "至少需要两次运行"}
          </div>
        </div>
      </div>

      {fanout.isPending ? (
        <div className="td-cmp-progress" role="status">
          <Loader2 size={16} className="spin" />
          <div>
            <div className="td-cmp-progress-title">
              正在扇出到 {fanout.variables?.length ?? selectedIds.length} 个 Agent…
            </div>
            <div className="td-cmp-progress-body">
              Agent 会依次执行；接口在全部运行结束后返回。已用时 {fmtDuration(elapsed)}。
            </div>
          </div>
        </div>
      ) : null}

      {fanout.isError ? (
        <div className="td-alert" role="alert">
          <span className="td-alert-icon">
            <AlertTriangle size={16} />
          </span>
          <div className="td-alert-main">
            <div className="td-alert-title">扇出请求失败</div>
            <div className="td-alert-body">{errorText(fanout.error)}</div>
          </div>
          <div className="td-alert-actions">
            <Button size="sm" variant="ghost" onClick={() => fanout.reset()}>
              关闭
            </Button>
          </div>
        </div>
      ) : null}

      {fanout.data ? (
        <Panel className="td-panel td-cmp-fanout-summary">
          <SectionHeader
            title={
              <span className="td-section-title">
                <Layers size={15} /> 最近一次扇出
              </span>
            }
            subtitle={fanout.data.best_agent ? `最佳结果：${agentLabel(fanout.data.best_agent)}` : "所有运行均已记录，可继续对比"}
            actions={
              <Button size="sm" variant="ghost" onClick={() => fanout.reset()}>
                <X size={12} /> 收起
              </Button>
            }
          />
          <div className="td-cmp-fanout-rows">
            {(fanout.data.results ?? []).map((result, index) => (
              <div key={`${result.agent_id ?? "agent"}-${index}`} className="td-cmp-fanout-row">
                <span className="td-cmp-fanout-agent">
                  <AgentAvatar
                    id={result.agent_id || "agent"}
                    label={agentLabel(result.agent_id || "agent")}
                  />
                  {shortLabel(agentLabel(result.agent_id || "agent"))}
                </span>
                <Badge tone={statusTone(result.status || "unknown")} dot>
                  {statusLabel(result.status || "unknown")}
                </Badge>
                <span className="td-cmp-fanout-detail">
                  {result.error
                    ? result.error
                    : result.changes?.length
                      ? `${result.changes.length} 个文件改动`
                      : "无文件改动"}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      ) : null}

      {compareQuery.isError ? (
        <div className="td-alert" role="alert">
          <span className="td-alert-icon">
            <AlertTriangle size={16} />
          </span>
          <div className="td-alert-main">
            <div className="td-alert-title">无法加载对比数据</div>
            <div className="td-alert-body">{errorText(compareQuery.error)}</div>
          </div>
          <div className="td-alert-actions">
            <Button size="sm" variant="ghost" onClick={() => void compareQuery.refetch()}>
              重试
            </Button>
          </div>
        </div>
      ) : null}

      <Panel className="td-cmp-section">
        <div className="td-cmp-section-head">
          <SectionHeader
            title={
              <span className="td-section-title">
                <Users size={15} /> 运行
              </span>
            }
            subtitle={
              displayRuns.length
                ? "按 Agent 分组，保留每个尝试的状态、耗时、Token 与退出码"
                : "同一任务的所有运行会出现在这里"
            }
          />
        </div>
        <div className="td-cmp-section-body">
          {displayRuns.length ? (
            <div className="td-cmp-runs-grid">
              {groups.map((group) => (
                <div key={group.agentId} className="td-cmp-agent">
                  <div className="td-cmp-agent-head">
                    <AgentAvatar id={group.agentId} label={group.label} />
                    <div className="td-cmp-agent-main">
                      <div className="td-cmp-agent-name">{group.label}</div>
                      <div className="td-cmp-agent-meta">
                        {group.protocol || "cli"}
                        {group.runs[0]?.role ? ` · ${group.runs[0].role}` : ""}
                      </div>
                    </div>
                    <Badge tone="outline">{group.runs.length} 次</Badge>
                  </div>
                  <div className="td-cmp-run-list">
                    {group.runs.map((run) => (
                      <div key={run.run_id} className="td-cmp-run">
                        <div className="td-cmp-run-head">
                          <span className="td-cmp-run-attempt">#{run.attempt || 1}</span>
                          <RunStatusBadge status={run.status} />
                          <span className="td-cmp-run-time">{fmtDateTime(run.started_at)}</span>
                          <button
                            type="button"
                            className="td-cmp-run-output"
                            onClick={() => setOutputRunId(run.run_id)}
                          >
                            <Terminal size={12} /> 输出
                          </button>
                        </div>
                        <div className="td-cmp-run-metrics">
                          <span className="td-cmp-run-metric">
                            <Timer size={11} /> {fmtDuration(liveDuration(run, now))}
                          </span>
                          <span className="td-cmp-run-metric">
                            <Layers size={11} /> {fmtInt(run.cost_tokens)} tokens
                          </span>
                          <span className="td-cmp-run-metric">
                            <span className="td-cmp-run-cost">{formatCost(run.cost_amount)}</span>
                          </span>
                          <span className="td-cmp-run-metric">
                            <Clock3 size={11} /> exit{" "}
                            {run.exit_code === null || run.exit_code === undefined
                              ? "—"
                              : run.exit_code}
                          </span>
                        </div>
                        {run.error ? (
                          <div className="td-cmp-run-error" title={run.error}>
                            {run.error}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={<GitCompare size={20} />}
              title="还没有可对比的运行"
              hint="先运行一次任务，或用扇出对比让多个 Agent 各跑一次。"
              action={
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => setFanoutOpen(true)}
                  disabled={Boolean(fanoutDisabledReason)}
                >
                  <GitCompare size={13} /> 扇出对比
                </Button>
              }
            />
          )}
        </div>
      </Panel>

      <Panel className="td-cmp-section">
        <div className="td-cmp-section-head">
          <SectionHeader
            title={
              <span className="td-section-title">
                <FileText size={15} /> 产物差异
              </span>
            }
            subtitle={
              displayRuns.length > 1
                ? "按文件对齐不同运行的产物；默认取每个 Agent 最近一次成功运行，失败 Agent 回退到最近一次"
                : "至少需要两次运行才能比较产物"
            }
            actions={
              <Segmented
                value={columnMode}
                ariaLabel="对比列范围"
                options={[
                  {
                    value: "latest",
                    label: "每 Agent 一次",
                    title: "优先取最近一次成功运行；失败 Agent 回退到最近一次运行",
                  },
                  { value: "all", label: `全部 ${displayRuns.length}` },
                ]}
                onChange={setColumnMode}
              />
            }
          />
        </div>
        <div className="td-cmp-section-body td-cmp-files-body">
          {displayRuns.length < 2 ? (
            <EmptyState
              icon={<GitCompare size={20} />}
              title="只有一次运行，暂无对比"
              hint="扇出到 2–4 个 Agent 后，这里会显示并排的文件列表与差异。"
              action={
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => setFanoutOpen(true)}
                  disabled={Boolean(fanoutDisabledReason)}
                >
                  <GitCompare size={13} /> 扇出对比
                </Button>
              }
            />
          ) : compareQuery.isPending ? (
            <div className="td-cmp-loading">
              <Spinner size={14} /> 正在加载产物对比…
            </div>
          ) : compareQuery.isError ? (
            <EmptyState
              icon={<AlertTriangle size={20} />}
              title="无法加载产物对比"
              hint={errorText(compareQuery.error)}
              action={
                <Button size="sm" variant="ghost" onClick={() => void compareQuery.refetch()}>
                  重试
                </Button>
              }
            />
          ) : files.length === 0 ? (
            <EmptyState
              icon={<FileText size={20} />}
              title="没有登记输出产物"
              hint="这些运行没有采集到 output 产物。可以查看运行输出、改动页，或检查任务的 outputs 配置。"
            />
          ) : (
            <>
              <div className="td-cmp-file-tools">
                <label className="td-cmp-search">
                  <Search size={13} />
                  <input
                    className="input"
                    value={fileQuery}
                    placeholder="筛选文件路径"
                    onChange={(event) => setFileQuery(event.target.value)}
                    aria-label="筛选产物文件"
                  />
                </label>
                <label className="td-cmp-check">
                  <input
                    type="checkbox"
                    checked={onlyDiffs}
                    onChange={(event) => setOnlyDiffs(event.target.checked)}
                  />
                  只看差异
                </label>
                <span className="td-cmp-file-count">
                  {rows.length} / {files.length} 个文件
                </span>
              </div>

              {columnMode === "latest" && columns.length < 2 && displayRuns.length > 1 ? (
                <div className="td-cmp-inline-note td-cmp-matrix-note">
                  当前每个 Agent 只有一次代表性运行。切换「全部」可以比较同一 Agent 的多次尝试。
                </div>
              ) : null}

              {rows.length ? (
                <div className="td-cmp-matrix-wrap">
                  <table className="td-cmp-matrix">
                    <thead>
                      <tr>
                        <th scope="col">文件</th>
                        {columns.map((run) => (
                          <th key={run.run_id} scope="col">
                            <span className="td-cmp-matrix-agent">
                              {shortLabel(run.agent_label || run.agent_id)}
                              <span>#{run.attempt || 1}</span>
                            </span>
                          </th>
                        ))}
                        <th scope="col">结论</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map(({ file, verdict }) => {
                        const covered = Object.keys(file.per_run ?? {}).length;
                        return (
                          <tr
                            key={file.rel_path}
                            className={clsx(expandedPath === file.rel_path && "is-selected")}
                          >
                            <td>
                              <button
                                type="button"
                                className="td-cmp-file-btn"
                                onClick={() =>
                                  setExpandedPath((previous) =>
                                    previous === file.rel_path ? null : file.rel_path,
                                  )
                                }
                                title={file.rel_path}
                              >
                                {expandedPath === file.rel_path ? (
                                  <ChevronDown size={12} />
                                ) : (
                                  <ChevronRight size={12} />
                                )}
                                <span className="td-cmp-file-path mono">{file.rel_path}</span>
                              </button>
                              <span className="td-cmp-file-sub">
                                {covered}/{displayRuns.length} 次运行产出
                              </span>
                            </td>
                            {columns.map((run) => {
                              const artifact = file.per_run?.[run.run_id];
                              return (
                                <td
                                  key={`${file.rel_path}-${run.run_id}`}
                                  className={clsx(!artifact && "is-missing")}
                                >
                                  {artifact ? (
                                    <span className="td-cmp-artifact">
                                      <span className="td-cmp-hash mono">
                                        {shortHash(artifact.sha256)}
                                      </span>
                                      <span className="td-cmp-size">
                                        {fmtBytes(artifact.size)}
                                      </span>
                                    </span>
                                  ) : (
                                    <span className="td-cmp-missing">未产出</span>
                                  )}
                                </td>
                              );
                            })}
                            <td>
                              <Badge tone={verdict.tone}>{verdict.label}</Badge>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="td-cmp-inline-empty">
                  没有匹配的文件。调整筛选条件，或取消「只看差异」。
                </div>
              )}

              {selectedFile ? (
                <div className="td-cmp-file-detail">
                  <div className="td-cmp-file-detail-head">
                    <div className="td-cmp-file-detail-title">
                      <FileText size={14} />
                      <span className="mono">{selectedFile.rel_path}</span>
                      <Badge tone={fileVerdict(selectedFile, columns, displayRuns.length).tone}>
                        {fileVerdict(selectedFile, columns, displayRuns.length).label}
                      </Badge>
                    </div>
                    <div className="td-cmp-file-detail-actions">
                      {selectedFileDiff ? (
                        <>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={async () => {
                              const ok = await copyText(selectedFileDiff);
                              if (ok) toast.success("差异文本已复制");
                              else toast.error("复制失败");
                            }}
                          >
                            <Copy size={12} /> 复制 diff
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() =>
                              downloadText(
                                `${selectedFile.rel_path.split(/[\\/]/).pop() || "diff"}.diff`,
                                selectedFileDiff,
                              )
                            }
                          >
                            <Download size={12} /> 下载
                          </Button>
                        </>
                      ) : null}
                      <IconButton label="关闭文件详情" onClick={() => setExpandedPath(null)}>
                        <X size={14} />
                      </IconButton>
                    </div>
                  </div>

                  <div className="td-cmp-file-cols">
                    {columns.map((run) => {
                      const artifact = selectedFile.per_run?.[run.run_id];
                      return (
                        <div key={`detail-${run.run_id}`} className="td-cmp-file-col">
                          <div className="td-cmp-file-col-head">
                            <span>{shortLabel(run.agent_label || run.agent_id)}</span>
                            <span className="td-cmp-run-attempt">#{run.attempt || 1}</span>
                            <RunStatusBadge status={run.status} />
                          </div>
                          {artifact ? (
                            <>
                              <div className="td-cmp-file-col-body">
                                <span className="mono">{shortHash(artifact.sha256, 16)}</span>
                                <span>{fmtBytes(artifact.size)}</span>
                              </div>
                              <div className="td-cmp-file-col-foot">
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => void openArtifact(artifact.abs_path)}
                                  disabled={!artifact.abs_path}
                                >
                                  <ExternalLink size={12} /> 打开产物
                                </Button>
                              </div>
                            </>
                          ) : (
                            <div className="td-cmp-file-col-missing">本次运行未产出该文件</div>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  {selectedFileDiff ? (
                    <DiffText text={selectedFileDiff} />
                  ) : (
                    <div className="td-cmp-inline-note">
                      当前对比接口只返回产物哈希与大小，没有附带 diff 文本。可打开两侧产物查看内容；
                      若后端后续提供 <span className="mono">diff</span> /{" "}
                      <span className="mono">diff_text</span> 字段，这里会自动显示。
                    </div>
                  )}
                </div>
              ) : null}
            </>
          )}
        </div>
      </Panel>

      {displayRuns.length > 1 && columns.length > 1 ? (
        <Panel className="td-cmp-section">
          <div className="td-cmp-section-head">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <Terminal size={15} /> 输出预览
                </span>
              }
              subtitle={`并排查看 ${previewColumns.length} 个运行的 stdout；默认最多 4 列，尾部 5000 字符`}
              actions={
                columns.length > 4 ? (
                  <Badge tone="outline">还有 {columns.length - 4} 列未展开</Badge>
                ) : undefined
              }
            />
          </div>
          <div className="td-cmp-output-grid">
            {previewColumns.map((run) => (
              <OutputPane
                key={`out-${run.run_id}`}
                run={run}
                now={now}
                onOpen={() => setOutputRunId(run.run_id)}
              />
            ))}
          </div>
        </Panel>
      ) : null}

      <Dialog
        open={fanoutOpen}
        onClose={() => {
          if (!fanout.isPending) setFanoutOpen(false);
        }}
        width={680}
        title="扇出对比"
        description="同一任务，多个 Agent 各跑一次；每个结果单独保留。"
        footer={
          <>
            <Button onClick={() => setFanoutOpen(false)} disabled={fanout.isPending}>
              取消
            </Button>
            <Button
              variant="primary"
              disabled={selectedIds.length < 2 || selectedIds.length > 4}
              onClick={() => {
                setFanoutOpen(false);
                setConfirmFanout(true);
              }}
            >
              下一步：确认扇出
            </Button>
          </>
        }
      >
        <div className="td-cmp-picker">
          <div className="td-cmp-picker-head">
            <span>
              已选 <strong>{selectedIds.length}</strong> / 4
            </span>
            <div className="td-cmp-picker-actions">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSelectedAgents(readyAgents.slice(0, 2).map((agent) => String(agent.id || agent.agent_id)))}
                disabled={readyAgents.length < 2}
              >
                选前两个
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSelectedAgents([])}
                disabled={!selectedAgents.length}
              >
                清空
              </Button>
            </div>
          </div>

          {agentsQuery.isPending ? (
            <div className="td-cmp-loading">
              <Spinner size={14} /> 正在读取 Agent 编队…
            </div>
          ) : agentsQuery.isError ? (
            <div className="td-cmp-out-error">
              <AlertTriangle size={14} />
              <span>{errorText(agentsQuery.error)}</span>
              <Button size="sm" variant="ghost" onClick={() => void agentsQuery.refetch()}>
                重试
              </Button>
            </div>
          ) : readyAgents.length < 2 ? (
            <EmptyState
              icon={<Users size={18} />}
              title="可用 Agent 不足"
              hint="至少需要 2 个已启用且命令可解析的 Agent。请到「Agent 引擎」检查配置。"
            />
          ) : (
            <div className="td-cmp-agent-picker">
              {readyAgents.map((agent) => {
                const id = String(agent.id || agent.agent_id);
                const active = selectedAgents.includes(id);
                const full = !active && selectedIds.length >= 4;
                return (
                  <button
                    key={id}
                    type="button"
                    className={clsx("td-cmp-agent-option", active && "is-selected")}
                    aria-pressed={active}
                    disabled={full}
                    onClick={() => toggleAgent(id)}
                  >
                    <span className="td-cmp-agent-check" aria-hidden="true">
                      {active ? <Check size={11} /> : null}
                    </span>
                    <AgentAvatar id={id} label={agent.label || id} />
                    <span className="td-cmp-agent-option-main">
                      <span className="td-cmp-agent-option-name">{agent.label || id}</span>
                      <span className="td-cmp-agent-option-meta">
                        {agent.protocol || "cli"}
                        {agent.roles?.length ? ` · ${agent.roles.join(" / ")}` : ""}
                        {agent.can_write === false ? " · 只读" : ""}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          <div className="td-cmp-callout">
            <Info size={14} />
            <div>
              <strong>隔离与写入</strong>
              <p>
                {task.isolation === "shared"
                  ? "当前任务是共享工作区模式：多个 Agent 会依次写入同一目录，后跑的运行可能覆盖前面产出的文件。建议先把任务的写入方式改为隔离副本，再做扇出对比。"
                  : "每个 Agent 会在独立的隔离副本中各跑一次，产物不会互相覆盖；完成后可在这里按 Agent、尝试和文件逐项比较。"}
              </p>
            </div>
          </div>
        </div>
      </Dialog>

      <ConfirmDialog
        open={confirmFanout}
        title={`确认扇出到 ${selectedIds.length} 个 Agent？`}
        message={
          <div className="td-cmp-confirm">
            <div className="td-cmp-confirm-agents">
              {selectedIds.map((id) => (
                <Badge key={id} tone="outline">
                  <span
                    className="td-cmp-confirm-dot"
                    style={{ background: agentColor(id) }}
                    aria-hidden="true"
                  />
                  {agentLabel(id)}
                </Badge>
              ))}
            </div>
            <p>
              这些 Agent 会依次在同一任务上各执行一次，每次运行都会保留自己的状态、日志与产物。
              {task.isolation === "shared"
                ? "当前为共享工作区模式，后跑的运行可能覆盖前面的文件。"
                : "每次执行使用独立隔离副本，不会互相覆盖。"}
            </p>
            <p className="td-cmp-confirm-hint">
              扇出请求会等待全部运行结束后返回，耗时可能较长；期间可保持页面打开。
            </p>
          </div>
        }
        confirmLabel="开始扇出"
        onCancel={() => setConfirmFanout(false)}
        onConfirm={() => {
          setConfirmFanout(false);
          fanout.mutate(selectedIds);
        }}
      />

      {outputRun ? (
        <RunOutputDialog run={outputRun} onClose={() => setOutputRunId(null)} />
      ) : null}
    </div>
  );
}
