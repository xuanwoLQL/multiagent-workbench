import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ClipboardCopy,
  Download,
  ExternalLink,
  RefreshCw,
  X,
} from "lucide-react";
import { RunStatusBadge } from "../../components/StatusBadge";
import { Button, IconButton, Segmented, Spinner } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { fmtDateTime, fmtDuration, fmtInt, stripAnsi } from "../../lib/format";
import type { RunRecord } from "./runs-api";
import { useRunOutput } from "./runs-api";
import {
  copyToClipboard,
  downloadText,
  formatChars,
  formatCost,
  runDurationMs,
  shortId,
  useNow,
} from "./runs-util";

type OutputTab = "prompt" | "stdout" | "stderr";

const TAB_LABEL: Record<OutputTab, string> = {
  prompt: "Prompt",
  stdout: "stdout",
  stderr: "stderr",
};

export function RunDetailPanel({
  run,
  taskTitle,
  onClose,
  onOpenTask,
}: {
  run: RunRecord;
  taskTitle: string;
  onClose: () => void;
  onOpenTask?: (id: string) => void;
}) {
  const toast = useToast();
  const [tab, setTab] = useState<OutputTab>("stdout");
  const output = useRunOutput(run.id);
  const now = useNow(run.status === "running");

  useEffect(() => {
    setTab("stdout");
  }, [run.id]);

  const duration = useMemo(() => {
    if (run.status === "running") {
      const started = run.started_at ? new Date(run.started_at).getTime() : NaN;
      if (!Number.isNaN(started)) return Math.max(runDurationMs(run), now - started);
    }
    return runDurationMs(run);
  }, [run, now]);

  const counts = useMemo(
    () => ({
      prompt: (output.data?.prompt ?? "").length,
      stdout: (output.data?.stdout ?? "").length,
      stderr: (output.data?.stderr ?? "").length,
    }),
    [output.data],
  );

  const raw = tab === "prompt" ? output.data?.prompt : tab === "stdout" ? output.data?.stdout : output.data?.stderr;
  const text = stripAnsi(raw ?? "");
  const tabLabel = TAB_LABEL[tab];

  const handleCopy = async () => {
    if (!text) return;
    const ok = await copyToClipboard(text);
    if (ok) toast.success("已复制到剪贴板", `${tabLabel} · ${fmtInt(text.length)} 字符`);
    else toast.error("复制失败", "浏览器阻止了剪贴板访问，请手动选择文本复制。");
  };

  const handleCopyId = async () => {
    const ok = await copyToClipboard(run.id);
    if (ok) toast.success("已复制运行 ID", run.id);
    else toast.error("复制失败", "浏览器阻止了剪贴板访问。");
  };

  const handleCopyCwd = async () => {
    if (!run.cwd) return;
    const ok = await copyToClipboard(run.cwd);
    if (ok) toast.success("已复制工作目录", run.cwd);
    else toast.error("复制失败", "浏览器阻止了剪贴板访问。");
  };

  const handleDownload = () => {
    if (!text) return;
    const filename = `run-${shortId(run.id, 10)}-${tab}.log`;
    downloadText(filename, text);
    toast.success("已开始下载", filename);
  };

  const emptyHint =
    tab === "prompt"
      ? "这次调用没有记录 Prompt。旧版本运行或使用只读回放时可能出现。"
      : tab === "stderr"
        ? "没有 stderr 输出，进程运行期间未写入错误流。"
        : "没有 stdout 输出，或运行尚未产生可回放的内容。";

  return (
    <aside className="run-detail" aria-label="运行详情">
      <header className="run-detail-head">
        <div className="run-detail-heading">
          <div className="run-detail-eyebrow">RUN · {shortId(run.id, 12)}</div>
          <h2 className="run-detail-title" title={taskTitle}>
            {taskTitle}
          </h2>
          <div className="run-detail-sub">
            <RunStatusBadge status={run.status} />
            <span className="mono">{run.agent_label || run.agent_id}</span>
            <span>第 {run.attempt} 次</span>
            {run.status === "running" ? (
              <span className="runs-live runs-live-sm">
                <span className="runs-live-dot" />
                进行中
              </span>
            ) : null}
          </div>
        </div>
        <IconButton label="关闭详情（Esc）" onClick={onClose}>
          <X size={15} />
        </IconButton>
      </header>

      <div className="run-detail-body">
        <div className="run-facts">
          <div className="run-fact">
            <span className="run-fact-label">Agent</span>
            <span className="run-fact-value">
              {run.agent_label || run.agent_id}
              {run.agent_label && run.agent_label !== run.agent_id ? (
                <span className="run-fact-sub mono">{run.agent_id}</span>
              ) : null}
            </span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">尝试</span>
            <span className="run-fact-value">第 {run.attempt} 次</span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">开始</span>
            <span className="run-fact-value">{fmtDateTime(run.started_at)}</span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">结束</span>
            <span className="run-fact-value">
              {run.finished_at ? fmtDateTime(run.finished_at) : run.status === "running" ? "仍在运行" : "—"}
            </span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">耗时</span>
            <span className="run-fact-value">{fmtDuration(duration)}</span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">Token</span>
            <span className="run-fact-value">{fmtInt(run.cost_tokens)}</span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">成本</span>
            <span className="run-fact-value">{formatCost(run.cost_amount)}</span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">退出码</span>
            <span className={run.exit_code ? "run-fact-value run-fact-bad mono" : "run-fact-value mono"}>
              {run.exit_code === null || run.exit_code === undefined ? "—" : run.exit_code}
            </span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">超时</span>
            <span className="run-fact-value">
              {run.timeout_sec ? fmtDuration(run.timeout_sec * 1000) : "—"}
            </span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">协议 / 角色</span>
            <span className="run-fact-value">
              {run.protocol || "—"} · {run.role || "—"}
            </span>
          </div>
          <div className="run-fact">
            <span className="run-fact-label">运行 ID</span>
            <span className="run-fact-value run-fact-inline">
              <span className="mono run-fact-mono">{run.id}</span>
              <IconButton label="复制运行 ID" onClick={() => void handleCopyId()}>
                <ClipboardCopy size={13} />
              </IconButton>
            </span>
          </div>
          {run.cwd ? (
            <div className="run-fact">
              <span className="run-fact-label">工作目录</span>
              <span className="run-fact-value run-fact-inline">
                <span className="mono run-fact-mono" title={run.cwd}>
                  {run.cwd}
                </span>
                <IconButton label="复制工作目录" onClick={() => void handleCopyCwd()}>
                  <ClipboardCopy size={13} />
                </IconButton>
              </span>
            </div>
          ) : null}
        </div>

        <div className="run-detail-links">
          {onOpenTask ? (
            <Button size="sm" variant="ghost" onClick={() => onOpenTask(run.task_id)}>
              <ExternalLink size={13} />
              打开任务上下文
            </Button>
          ) : null}
          <Button size="sm" variant="ghost" onClick={() => setTab("prompt")}>
            <ClipboardCopy size={13} />
            查看 Prompt
          </Button>
        </div>

        {run.error ? (
          <div className="run-error" role="alert">
            <AlertTriangle size={14} />
            <span>{run.error}</span>
          </div>
        ) : null}

        <div className="run-output">
          <div className="run-output-head">
            <Segmented<OutputTab>
              value={tab}
              ariaLabel="运行输出"
              onChange={setTab}
              options={[
                {
                  value: "prompt",
                  label: (
                    <>
                      Prompt <span className="run-tab-count">{formatChars(counts.prompt)}</span>
                    </>
                  ),
                },
                {
                  value: "stdout",
                  label: (
                    <>
                      stdout <span className="run-tab-count">{formatChars(counts.stdout)}</span>
                    </>
                  ),
                },
                {
                  value: "stderr",
                  label: (
                    <>
                      stderr <span className="run-tab-count">{formatChars(counts.stderr)}</span>
                    </>
                  ),
                },
              ]}
            />
            <div className="run-output-tools">
              <IconButton label={`复制 ${tabLabel}`} onClick={() => void handleCopy()} disabled={!text}>
                <ClipboardCopy size={14} />
              </IconButton>
              <IconButton label={`下载 ${tabLabel}`} onClick={handleDownload} disabled={!text}>
                <Download size={14} />
              </IconButton>
              <IconButton label="重新读取输出" onClick={() => void output.refetch()}>
                <RefreshCw size={14} className={output.isFetching ? "spin" : undefined} />
              </IconButton>
            </div>
          </div>

          {output.isLoading ? (
            <div className="run-output-loading">
              <Spinner size={14} />
              正在读取 {tabLabel}…
            </div>
          ) : output.isError ? (
            <div className="run-output-error" role="alert">
              <AlertTriangle size={14} />
              <div className="run-output-error-text">
                <div>无法读取运行输出</div>
                <div className="run-output-error-detail">{(output.error as Error)?.message}</div>
              </div>
              <Button size="sm" variant="ghost" onClick={() => void output.refetch()}>
                重试
              </Button>
            </div>
          ) : text ? (
            <pre className="run-output-pre">{text}</pre>
          ) : (
            <div className="run-output-empty">{emptyHint}</div>
          )}

          {text ? (
            <div className="run-output-note">
              {fmtInt(text.length)} 字符 · 从本地运行目录只读回放
            </div>
          ) : null}
        </div>
      </div>
    </aside>
  );
}
