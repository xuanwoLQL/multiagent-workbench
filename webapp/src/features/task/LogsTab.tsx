import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  AlertCircle,
  Copy,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Terminal,
  WrapText,
} from "lucide-react";
import { api } from "../../api/client";
import type { Run } from "../../api/types";
import { RunStatusBadge } from "../../components/StatusBadge";
import { Badge, Button, EmptyState } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { useLocalStorage } from "../../lib/hooks";
import { fmtDateTime, fmtDuration, fmtInt, stripAnsi } from "../../lib/format";
import {
  asArray,
  copyText,
  downloadText,
  runDuration,
  sortRuns,
  statusLabel,
} from "./helpers";

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

type Channel = "stdout" | "stderr";

const MAX_LINES = 3000;

function lineCount(text: string): number {
  if (!text) return 0;
  return text.split("\n").length;
}

export function LogsTab({
  taskId,
  runs,
  selectedRunId,
  onSelectRun,
}: {
  taskId: string;
  runs: Run[];
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
}) {
  const toast = useToast();
  const ordered = useMemo(() => sortRuns(asArray(runs)), [runs]);
  const selected =
    ordered.find((run) => run.id === selectedRunId) ?? ordered[0] ?? null;
  const [channel, setChannel] = useState<Channel>("stdout");
  const [wrapped, setWrapped] = useLocalStorage("multiagent.logs.wrap", false);
  const touched = useRef(false);

  const outputQuery = useQuery({
    queryKey: ["run-output", selected?.id ?? ""],
    queryFn: () => api.get<RunOutput>(`/api/runs/${selected?.id}/output?limit=200000`),
    enabled: Boolean(selected?.id),
    retry: false,
    refetchInterval: selected?.status === "running" ? 3000 : false,
  });

  useEffect(() => {
    setChannel("stdout");
    touched.current = false;
  }, [selected?.id]);

  useEffect(() => {
    if (touched.current || !outputQuery.data) return;
    if (!outputQuery.data.stdout && outputQuery.data.stderr) {
      setChannel("stderr");
    }
  }, [outputQuery.data]);

  const stdout = useMemo(() => stripAnsi(outputQuery.data?.stdout ?? ""), [outputQuery.data]);
  const stderr = useMemo(() => stripAnsi(outputQuery.data?.stderr ?? ""), [outputQuery.data]);
  const activeText = channel === "stdout" ? stdout : stderr;
  const allLines = useMemo(() => activeText.split("\n"), [activeText]);
  const truncated = allLines.length > MAX_LINES;
  const lines = truncated ? allLines.slice(0, MAX_LINES) : allLines;
  const counts = useMemo(
    () => ({ stdout: lineCount(stdout), stderr: lineCount(stderr) }),
    [stdout, stderr],
  );
  const prompt = outputQuery.data?.prompt ?? "";
  const run = selected;
  const errorText = run?.error || outputQuery.data?.run?.error || "";
  const meta = outputQuery.data?.run;

  const chooseChannel = (next: Channel) => {
    touched.current = true;
    setChannel(next);
  };

  const copyActive = async () => {
    if (!activeText) {
      toast.info("当前输出为空");
      return;
    }
    const ok = await copyText(activeText);
    if (ok) toast.success(`已复制 ${channel}`);
    else toast.error("复制失败");
  };

  const downloadActive = () => {
    if (!activeText) {
      toast.info("当前输出为空");
      return;
    }
    downloadText(
      `task-${taskId}-${run?.id ?? "run"}-${channel}.log`,
      activeText,
      "text/plain;charset=utf-8",
    );
    toast.success("已开始下载", `${channel}.log`);
  };

  if (!ordered.length) {
    return (
      <div className="td-logs">
        <EmptyState
          icon={<Terminal size={22} />}
          title="还没有运行记录"
          hint="任务执行后，每次 Agent 调用的标准输出、错误输出与退出码都会保存在这里。"
        />
      </div>
    );
  }

  return (
    <div className="td-logs">
      <div className="td-logs-head">
        <div className="td-logs-title">
          <Terminal size={15} /> 运行日志
          <Badge tone="outline">{ordered.length} 次运行</Badge>
        </div>
        <div className="td-logs-picker">
          <select
            className="select td-run-select"
            value={selected?.id ?? ""}
            aria-label="选择运行记录"
            onChange={(event) => onSelectRun(event.target.value)}
          >
            {ordered.map((item) => (
              <option key={item.id} value={item.id}>
                {`${item.agent_label || item.agent_id} · 第 ${item.attempt || 1} 次 · ${statusLabel(
                  item.status,
                )} · ${fmtDateTime(item.started_at)}`}
              </option>
            ))}
          </select>
        </div>
      </div>

      {run ? (
        <div className="td-run-summary">
          <span className="td-run-summary-agent">
            <Terminal size={13} />
            {run.agent_label || run.agent_id}
          </span>
          <RunStatusBadge status={run.status} />
          <span>attempt #{run.attempt || 1}</span>
          <span>耗时 {runDuration(run) ? fmtDuration(runDuration(run)) : "—"}</span>
          <span>
            exit{" "}
            {run.exit_code !== null && run.exit_code !== undefined ? run.exit_code : "—"}
          </span>
          <span>{fmtInt(meta?.cost_tokens ?? run.cost_tokens)} tokens</span>
          <span className="td-run-summary-time">{fmtDateTime(run.started_at)}</span>
          {selected?.status === "running" ? (
            <span className="td-live-pill">
              <span className="td-live-dot" /> 实时刷新
            </span>
          ) : null}
        </div>
      ) : null}

      {errorText ? (
        <div className="td-log-error">
          <AlertCircle size={15} />
          <div>
            <div className="td-log-error-title">运行错误</div>
            <div className="td-log-error-body">{errorText}</div>
          </div>
        </div>
      ) : null}

      <div className="td-log-toolbar">
        <div className="td-log-tabs" role="tablist" aria-label="日志通道">
          <button
            type="button"
            role="tab"
            aria-selected={channel === "stdout"}
            className={clsx("td-log-tab", channel === "stdout" && "is-active")}
            onClick={() => chooseChannel("stdout")}
          >
            标准输出
            <span>{counts.stdout}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={channel === "stderr"}
            className={clsx(
              "td-log-tab",
              channel === "stderr" && "is-active",
              stderr && "has-error",
            )}
            onClick={() => chooseChannel("stderr")}
          >
            错误输出
            <span>{counts.stderr}</span>
          </button>
        </div>
        <div className="td-log-actions">
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
            aria-label="复制当前输出"
            title="复制"
            onClick={() => void copyActive()}
          >
            <Copy size={14} />
          </button>
          <button
            type="button"
            className="td-icon-btn"
            aria-label="下载当前输出"
            title="下载"
            onClick={downloadActive}
          >
            <Download size={14} />
          </button>
          <button
            type="button"
            className="td-icon-btn"
            aria-label="刷新输出"
            title="刷新"
            onClick={() => void outputQuery.refetch()}
          >
            <RefreshCw size={14} className={outputQuery.isFetching ? "spin" : undefined} />
          </button>
        </div>
      </div>

      <div className="td-log-viewer">
        <div className="td-log-viewer-head">
          <span className="mono">{channel}</span>
          <span>
            {counts[channel]} 行 · {activeText.length.toLocaleString("zh-CN")} 字符
            {truncated ? ` · 仅显示前 ${MAX_LINES} 行` : ""}
          </span>
        </div>
        {outputQuery.isLoading ? (
          <div className="td-log-loading">
            <Loader2 size={16} className="spin" /> 正在读取输出…
          </div>
        ) : outputQuery.isError ? (
          <div className="td-log-failure">
            <AlertCircle size={16} />
            <div>
              <div>无法读取这次运行的输出</div>
              <div className="td-log-failure-detail">
                {(outputQuery.error as Error)?.message || "接口不可用"}
              </div>
            </div>
            <Button size="sm" onClick={() => void outputQuery.refetch()}>
              重试
            </Button>
          </div>
        ) : activeText ? (
          <div className={clsx("td-log-body", wrapped && "is-wrap")}>
            {lines.map((line, index) => (
              <div key={index} className="td-log-line">
                <span className="td-log-no">{index + 1}</span>
                <span className="td-log-text">{line || " "}</span>
              </div>
            ))}
            {truncated ? (
              <div className="td-log-truncated">
                已截断显示前 {MAX_LINES} 行。完整内容请下载日志文件。
              </div>
            ) : null}
          </div>
        ) : (
          <div className="td-log-empty">
            <Terminal size={20} />
            <div>{channel} 为空</div>
            <div className="td-log-empty-hint">
              这次运行没有写入{channel === "stdout" ? "标准输出" : "错误输出"}。
            </div>
          </div>
        )}
      </div>

      {prompt ? (
        <details className="td-log-prompt">
          <summary>
            <FileText size={13} /> 本次输入提示词
            <span>{prompt.length.toLocaleString("zh-CN")} 字符</span>
          </summary>
          <pre>{prompt}</pre>
        </details>
      ) : null}
    </div>
  );
}
