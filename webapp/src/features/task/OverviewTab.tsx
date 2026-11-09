import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  CircleDollarSign,
  Clock3,
  Cpu,
  ExternalLink,
  FileCode2,
  FileDiff,
  FileImage,
  FileText,
  FolderOpen,
  GitBranch,
  History,
  Info,
  Layers,
  ListChecks,
  Package,
  RotateCcw,
  ShieldCheck,
  Timer,
  Wrench,
} from "lucide-react";
import { api, qk } from "../../api/client";
import type {
  Artifact,
  MergeInfo,
  Run,
  Task,
  TaskCostInfo,
  TaskDetail,
  TaskGitInfo,
  TaskIsolationInfo,
  TaskProcessInfo,
} from "../../api/types";
import { RunStatusBadge, StatusBadge } from "../../components/StatusBadge";
import { Badge, Button, EmptyState, Field, Panel, SectionHeader, Stat, Textarea } from "../../ui/primitives";
import { Dialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import {
  agentColor,
  fmtBytes,
  fmtDateTime,
  fmtDuration,
  fmtInt,
  initials,
} from "../../lib/format";
import {
  asArray,
  describeAcceptance,
  fileName,
  dirName,
  isImagePath,
  normalizeHistory,
  normalizeMultilineText,
  openLocalPath,
  runDuration,
  sortRuns,
  statusLabel,
  taskCostInfo,
  taskGitInfo,
  taskIsolationInfo,
  taskMergeInfo,
  taskMergeState,
  taskProcessInfo,
  totalRuntime,
  totalTokens,
  type MergeState,
  type TaskTab,
} from "./helpers";
import "./overview.css";

interface AcceptanceCheckView {
  type?: string;
  ok?: boolean;
  detail?: string;
  condition?: Record<string, unknown>;
  candidates?: string[];
  suggested?: Record<string, unknown> | null;
  hint?: string | null;
}

interface MergeSummary {
  text: string;
  files: string[];
  tone: "success" | "danger" | "warning" | "info" | "neutral";
}

function ArtifactIcon({ path }: { path?: string }) {
  if (isImagePath(path)) return <FileImage size={15} />;
  const ext = String(path ?? "").toLowerCase();
  if (/\.(ts|tsx|js|jsx|py|java|c|cc|cpp|h|hpp|cs|go|rs|rb|php|css|scss|html|json|ya?ml|toml|sql|sh)$/.test(ext)) {
    return <FileCode2 size={15} />;
  }
  return <FileText size={15} />;
}

function HandoffValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    return (
      <ul className="td-handoff-list">
        {value.map((item, index) => (
          <li key={index}>{typeof item === "string" ? item : JSON.stringify(item)}</li>
        ))}
      </ul>
    );
  }
  if (value && typeof value === "object") {
    return <pre className="td-handoff-json">{JSON.stringify(value, null, 2)}</pre>;
  }
  return <p className="td-handoff-text">{normalizeMultilineText(value)}</p>;
}

function artifactMeta(artifact: Artifact): string {
  const parts = [fmtBytes(artifact.size)];
  if (artifact.sha256) parts.push(artifact.sha256.slice(0, 10));
  return parts.join(" · ");
}

function acceptanceChecks(task: Task): AcceptanceCheckView[] {
  const raw = task.result?.acceptance;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  const checks = (raw as { checks?: unknown }).checks;
  if (!Array.isArray(checks)) return [];
  return checks.filter(
    (item): item is AcceptanceCheckView => Boolean(item) && typeof item === "object",
  );
}

function checkCandidates(check: AcceptanceCheckView): string[] {
  if (!Array.isArray(check.candidates)) return [];
  return check.candidates.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function checkSuggestion(check: AcceptanceCheckView): Record<string, unknown> | null {
  const suggested = check.suggested;
  if (!suggested || typeof suggested !== "object" || Array.isArray(suggested)) return null;
  return suggested;
}

function replaceAcceptanceCondition(
  conditions: unknown[],
  check: AcceptanceCheckView,
): unknown[] | null {
  const suggested = checkSuggestion(check);
  if (!suggested) return null;
  const source =
    check.condition && typeof check.condition === "object" && !Array.isArray(check.condition)
      ? check.condition
      : null;
  const targetType = String(source?.type ?? check.type ?? "");
  const hasPath = source ? Object.prototype.hasOwnProperty.call(source, "path") : false;
  const targetPath = hasPath ? String(source?.path ?? "") : "";
  let replaced = false;
  const next = conditions.map((item) => {
    if (replaced) return item;
    if (!item || typeof item !== "object" || Array.isArray(item)) return item;
    const record = item as Record<string, unknown>;
    if (String(record.type ?? "") !== targetType) return item;
    if (hasPath && String(record.path ?? "") !== targetPath) return item;
    replaced = true;
    return { ...suggested };
  });
  return replaced ? next : null;
}

function mergeSummary(state: MergeState, git: TaskGitInfo): MergeSummary {
  const merge: MergeInfo | null = git.merge ?? null;
  const sha = String(merge?.sha || git.commit || "");
  const branch = String(merge?.branch || git.branch || "");
  switch (state) {
    case "merged":
      return {
        text: `已合并到 ${branch || "基线分支"}${sha ? ` @${sha.slice(0, 8)}` : ""}`,
        files: [...(merge?.files ?? [])],
        tone: "success",
      };
    case "conflict":
      return {
        text: "合并冲突（已自动回滚）",
        files: [...(merge?.files ?? [])],
        tone: "danger",
      };
    case "dirty_blocked":
      return {
        text: "合并被阻止：基线工作树有未提交改动",
        files: [...(merge?.dirty_files ?? [])],
        tone: "warning",
      };
    case "blocked":
      return {
        text: "合并被阻止",
        files: [...(merge?.files ?? [])],
        tone: "warning",
      };
    case "discarded":
      return {
        text: "任务分支已放弃（未合并）",
        files: [],
        tone: "warning",
      };
    case "auto_merge_disabled":
      return {
        text: "自动合并已关闭，任务分支等待人工合并",
        files: [],
        tone: "info",
      };
    case "not_merged":
    case "pending":
    default:
      return { text: "尚未合并", files: [], tone: "neutral" };
  }
}

function mergeStateLabel(state: MergeState): string {
  switch (state) {
    case "merged":
      return "已合并";
    case "conflict":
      return "合并冲突";
    case "dirty_blocked":
      return "基线脏树阻断";
    case "blocked":
      return "合并被阻止";
    case "discarded":
      return "已放弃分支";
    case "auto_merge_disabled":
      return "待人工合并";
    default:
      return "尚未合并";
  }
}

function mergeTone(state: MergeState): "success" | "danger" | "warning" | "info" | "outline" {
  switch (state) {
    case "merged":
      return "success";
    case "conflict":
      return "danger";
    case "dirty_blocked":
    case "blocked":
    case "discarded":
      return "warning";
    case "auto_merge_disabled":
      return "info";
    default:
      return "outline";
  }
}

function fmtAmount(value: unknown): string {
  const amount = Number(value);
  return Number.isFinite(amount) ? amount.toFixed(2) : "—";
}

function costAmountText(cost: TaskCostInfo): string {
  const source = String(cost.source ?? "");
  const amount = Number(cost.amount);
  const tokens = Number(cost.tokens);
  if (source === "unreported") return "—";
  if (source === "reported" && amount === 0 && tokens > 0) return "费用未提供";
  if (source === "estimated") return `≈ ${fmtAmount(cost.amount)}`;
  return fmtAmount(cost.amount);
}

function costStatHint(cost: TaskCostInfo): string {
  const source = String(cost.source ?? "");
  const amount = Number(cost.amount);
  const tokens = Number(cost.tokens);
  if (source === "unreported") return "未上报（Agent 协议未提供成本数据）";
  if (source === "reported") {
    if (amount === 0 && tokens > 0) return "费用未提供";
    return `成本 ${fmtAmount(cost.amount)} · 已上报`;
  }
  if (source === "estimated") return `成本 ≈ ${fmtAmount(cost.amount)} · 估算`;
  return `成本 ${fmtAmount(cost.amount)} · 来源未知`;
}

function costSourceLabel(cost: TaskCostInfo): string {
  const source = String(cost.source ?? "");
  if (source === "reported") return "已上报";
  if (source === "unreported") return "未上报";
  if (source === "estimated") return "估算";
  return "来源未知";
}

function ProcessList({
  entries,
}: {
  entries: Array<{ pid: number; port?: number | null; command?: string }>;
}) {
  return (
    <ul className="td-process-list">
      {entries.map((entry, index) => (
        <li key={`${entry.pid}-${index}`} className="td-process-item">
          <span className="td-process-pid mono">pid {entry.pid}</span>
          {typeof entry.port === "number" ? (
            <span className="td-process-port">端口 {entry.port}</span>
          ) : (
            <span className="td-process-port is-muted">端口未识别</span>
          )}
          {entry.command ? (
            <code className="td-process-command" title={entry.command}>
              {entry.command}
            </code>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function OverviewTab({
  detail,
  onTab,
  onOpenLogs,
  onRetry,
  retryPending,
}: {
  detail: TaskDetail;
  onTab: (tab: TaskTab) => void;
  onOpenLogs: (runId?: string) => void;
  onRetry: () => void;
  retryPending?: boolean;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const task = detail.task;
  const runs = sortRuns(asArray(detail.runs) as Run[]);
  const changes = asArray(detail.changes);
  const [showAllArtifacts, setShowAllArtifacts] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [correctionText, setCorrectionText] = useState("");
  const [correctionError, setCorrectionError] = useState("");
  const [correctionSaving, setCorrectionSaving] = useState(false);
  const artifacts = [...(asArray(detail.artifacts) as Artifact[])].sort((left, right) => {
    const a = left.created_at ? new Date(left.created_at).getTime() : 0;
    const b = right.created_at ? new Date(right.created_at).getTime() : 0;
    return b - a;
  });
  const visibleArtifacts = showAllArtifacts ? artifacts : artifacts.slice(0, 12);
  const history = normalizeHistory(detail.history).slice(-8).reverse();
  const rawAcceptance = Array.isArray(task.acceptance)
    ? task.acceptance
    : task.acceptance
      ? [task.acceptance]
      : [];
  const handoff = (detail.handoff ?? null) as Record<string, unknown> | null;
  const handoffSections: Array<{ key: string; aliases?: string[]; label: string }> = [
    { key: "did", label: "已完成" },
    { key: "did_not", aliases: ["not_done"], label: "未完成" },
    { key: "issues", aliases: ["open_issues"], label: "遗留问题" },
    { key: "next_steps", aliases: ["next"], label: "下一步" },
    { key: "notes", label: "备注" },
  ];
  const lastError = String(task.last_error || task.blocked_reason || "").trim();
  const showError = Boolean(lastError) || task.status === "failed" || task.status === "blocked";
  // merge_* 阻塞只能通过「重试合并 / 重建分支 / 放弃任务」处理，
  // 普通重试必然被后端 409 拒绝，因此不渲染该入口。
  const mergeBlocked =
    task.status === "blocked" && String(task.blocked_reason || "").startsWith("merge_");
  const runCost = runs.reduce((sum, run) => sum + (Number(run.cost_amount) || 0), 0);
  const git: TaskGitInfo | null = taskGitInfo(task);
  const merge: MergeInfo | null = taskMergeInfo(task);
  const mergeState = taskMergeState(task);
  const gitMerge = git && mergeState ? mergeSummary(mergeState, git) : null;
  // 分支有提交但未合并：下游任务读的是基线，因此必须在概览里明确提示。
  // 只覆盖这三个「等待合并」状态；merged / conflict / blocked / discarded 保持原文案。
  const unmergedGitState =
    mergeState === "auto_merge_disabled" ||
    mergeState === "not_merged" ||
    mergeState === "pending";
  const isolation: TaskIsolationInfo | null = taskIsolationInfo(task);
  const processes: TaskProcessInfo | null = taskProcessInfo(task);
  const startedProcesses = asArray(processes?.started);
  const reapedProcesses = asArray(processes?.reaped);
  const processWarnings = asArray(processes?.warnings);
  const costInfo = taskCostInfo(task);
  const costHint = costInfo
    ? costStatHint(costInfo)
    : runs.length
      ? "历史数据（来源未知）"
      : "尚无成本数据";
  const acceptanceResult =
    task.result?.acceptance && typeof task.result.acceptance === "object" && !Array.isArray(task.result.acceptance)
      ? (task.result.acceptance as { ok?: boolean; summary?: string })
      : null;
  const checks = acceptanceChecks(task);
  const diagnostics = checks.filter((check) => {
    if (check.ok !== false) return false;
    return checkCandidates(check).length > 0 || Boolean(checkSuggestion(check)) || Boolean(check.hint);
  });
  const suggestionChecks = checks.filter(
    (check) => check.ok === false && Boolean(checkSuggestion(check)),
  );

  const openCorrection = (check?: AcceptanceCheckView) => {
    let text = JSON.stringify(task.acceptance ?? [], null, 2);
    if (check && checkSuggestion(check)) {
      try {
        const parsed: unknown = JSON.parse(text);
        if (Array.isArray(parsed)) {
          const next = replaceAcceptanceCondition(parsed, check);
          if (next) text = JSON.stringify(next, null, 2);
        }
      } catch {
        /* keep the current dictionary if it is not valid JSON */
      }
    }
    setCorrectionText(text);
    setCorrectionError("");
    setCorrectionOpen(true);
  };

  const adoptSuggestion = (check: AcceptanceCheckView) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(correctionText);
    } catch (error) {
      setCorrectionError(
        `JSON 解析失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return;
    }
    if (!Array.isArray(parsed)) {
      setCorrectionError("验收条件必须是数组（JSON array），不能是对象或字符串。");
      return;
    }
    const next = replaceAcceptanceCondition(parsed, check);
    if (!next) {
      setCorrectionError("没有找到对应的失败条件（type + path），请手动修改。");
      return;
    }
    setCorrectionText(JSON.stringify(next, null, 2));
    setCorrectionError("");
  };

  const saveAcceptance = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(correctionText);
    } catch (error) {
      setCorrectionError(
        `JSON 解析失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return;
    }
    if (!Array.isArray(parsed)) {
      setCorrectionError("验收条件必须是数组（JSON array），不能是对象或字符串。");
      return;
    }
    if (!parsed.length) {
      setCorrectionError("验收条件不能为空；请至少保留一条条件。");
      return;
    }
    setCorrectionSaving(true);
    try {
      await api.patch(`/api/tasks/${task.id}`, { acceptance: parsed });
      toast.success("验收条件已更新", "重新运行后按新条件判定，不会自动放行");
      setCorrectionOpen(false);
      await queryClient.invalidateQueries({ queryKey: qk.task(task.id) });
    } catch (error) {
      toast.error("保存验收条件失败", error instanceof Error ? error.message : String(error));
    } finally {
      setCorrectionSaving(false);
    }
  };

  const openArtifact = async (artifact: Artifact) => {
    const target = artifact.abs_path || artifact.rel_path || "";
    if (!target) {
      toast.error("没有可打开的路径");
      return;
    }
    const result = await openLocalPath(target);
    if (result === "desktop") {
      toast.success(`已打开 ${fileName(artifact.rel_path || artifact.abs_path)}`);
    } else if (result === "copied") {
      toast.info("已复制文件路径", target);
    } else {
      toast.error("无法打开文件", target);
    }
  };

  return (
    <div className="td-overview">
      {showError ? (
        <div className="td-alert" role="alert">
          <span className="td-alert-icon">
            <AlertTriangle size={17} />
          </span>
          <div className="td-alert-main">
            <div className="td-alert-title">
              {task.status === "blocked" ? "任务被阻塞" : "执行未通过验收"}
            </div>
            <div className="td-alert-body">
              {lastError || "运行结束但验收条件未满足。打开运行日志查看失败原因。"}
            </div>
          </div>
          <div className="td-alert-actions">
            <Button size="sm" variant="ghost" onClick={() => onOpenLogs()}>
              查看日志
            </Button>
            {mergeBlocked ? (
              <Badge tone="warning">请先处理合并冲突或重建分支</Badge>
            ) : (
              <Button size="sm" onClick={onRetry} loading={retryPending}>
                重试
              </Button>
            )}
          </div>
        </div>
      ) : null}

      <div className="td-stat-strip">
        <Stat label="尝试次数" value={runs.length} hint={`上限 ${task.max_attempts ?? "—"}`} />
        <Stat
          label="累计耗时"
          value={runs.length ? fmtDuration(totalRuntime(runs)) : "—"}
          hint={`当前：${task.status === "running" ? "计时中" : statusLabel(task.status)}`}
        />
        <Stat
          label="Token"
          value={fmtInt(totalTokens(runs))}
          hint={costHint}
        />
        <Stat
          label="产物"
          value={artifacts.length}
          hint={artifacts.length ? "可打开或定位" : "暂无产出文件"}
        />
      </div>

      <div className="td-overview-grid">
        <div className="td-overview-main">
          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <FileText size={15} /> 任务说明
                </span>
              }
              subtitle={task.outputs?.length ? `产出：${task.outputs.join(", ")}` : "给 Agent 的完整指令与验收标准"}
            />
            {task.brief?.trim() ? (
              <div className="td-brief">{task.brief}</div>
            ) : (
              <div className="td-inline-empty">这条任务没有单独说明，Agent 将只收到标题与产出要求。</div>
            )}
          </Panel>

          {git && mergeState && gitMerge ? (
            <Panel className="td-panel">
              <SectionHeader
                title={
                  <span className="td-section-title">
                    <GitBranch size={15} /> Git 交付
                  </span>
                }
                subtitle={git.error ? `git 操作失败：${git.error}` : "任务分支、提交与合并状态"}
                actions={<Badge tone={mergeTone(mergeState)}>{mergeStateLabel(mergeState)}</Badge>}
              />
              <dl className="td-kv">
                <div>
                  <dt>分支</dt>
                  <dd className="mono" title={git.branch || undefined}>
                    {git.branch || "—"}
                  </dd>
                </div>
                <div>
                  <dt>提交</dt>
                  <dd className="mono" title={git.commit || undefined}>
                    {git.commit ? String(git.commit).slice(0, 8) : "—"}
                    {git.files?.length ? (
                      <span className="td-git-file-count"> · {git.files.length} 个文件</span>
                    ) : null}
                  </dd>
                </div>
                <div>
                  <dt>基线</dt>
                  <dd className="mono" title={git.base || undefined}>
                    {git.base ? String(git.base).slice(0, 8) : "—"}
                  </dd>
                </div>
                <div>
                  <dt>自动合并</dt>
                  <dd>
                    {git.auto_merge === true
                      ? "开启"
                      : git.auto_merge === false
                        ? "关闭"
                        : "未记录"}
                  </dd>
                </div>
              </dl>
              <div className={`td-merge-state is-${gitMerge.tone}`}>
                <div className="td-merge-state-head">
                  {mergeState === "merged" ? (
                    <CheckCircle2 size={14} />
                  ) : mergeState === "conflict" ||
                    mergeState === "blocked" ||
                    mergeState === "dirty_blocked" ? (
                    <AlertTriangle size={14} />
                  ) : (
                    <Info size={14} />
                  )}
                  <span className="td-merge-state-title">{gitMerge.text}</span>
                </div>
                {mergeState === "merged" ? (
                  <div className="td-merge-state-meta">
                    文件数：{gitMerge.files.length} 个
                  </div>
                ) : null}
                {mergeState === "auto_merge_disabled" ? (
                  <div className="td-merge-state-meta">
                    分支：<code>{String(merge?.branch || git.branch || "—")}</code>
                  </div>
                ) : null}
                {mergeState === "blocked" && merge?.reason ? (
                  <div className="td-merge-state-meta">原因：{merge.reason}</div>
                ) : null}
                {gitMerge.files.length ? (
                  <ul className="td-merge-files">
                    {gitMerge.files.slice(0, 20).map((file) => (
                      <li key={file}>
                        <code title={file}>{file}</code>
                      </li>
                    ))}
                    {gitMerge.files.length > 20 ? (
                      <li className="td-merge-files-more">… 共 {gitMerge.files.length} 个文件</li>
                    ) : null}
                  </ul>
                ) : null}
                {merge?.error && mergeState !== "dirty_blocked" ? (
                  <div className="td-merge-error">{merge.error}</div>
                ) : null}
              </div>
              {unmergedGitState ? (
                <div className="td-merge-warning" role="status">
                  <AlertTriangle size={14} />
                  <div className="td-merge-warning-main">
                    <div className="td-merge-warning-title">
                      改动尚未合并到基线；依赖此任务的下游任务看不到这些产出。
                    </div>
                    <div className="td-merge-warning-body">
                      请先在任务详情/审阅页执行「合并到基线」，或在 Settings 开启 auto_merge。
                    </div>
                    {git.auto_merge === false ? (
                      <div className="td-merge-warning-policy">当前策略：先审后合（默认）</div>
                    ) : null}
                  </div>
                </div>
              ) : null}
              <p className="td-git-note">
                合并使用 <code>git merge --no-ff</code>；发生冲突时会自动中止并回滚，不会自动解决冲突。
              </p>
            </Panel>
          ) : null}

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <ShieldCheck size={15} /> 隔离环境
                </span>
              }
              subtitle={
                isolation
                  ? `${isolation.requested_mode || task.isolation} → ${isolation.mode || "未知模式"}（请求 → 实际）`
                  : "任务声明的隔离模式"
              }
              actions={
                isolation ? (
                  isolation.degraded ? (
                    <Badge tone="warning">已降级</Badge>
                  ) : (
                    <Badge tone="outline">{isolation.mode || task.isolation}</Badge>
                  )
                ) : (
                  <Badge tone="outline">{task.isolation}</Badge>
                )
              }
            />
            {isolation ? (
              <>
                <dl className="td-kv">
                  <div>
                    <dt>请求</dt>
                    <dd className="mono">{isolation.requested_mode || task.isolation}</dd>
                  </div>
                  <div>
                    <dt>实际</dt>
                    <dd className="mono">{isolation.mode || "—"}</dd>
                  </div>
                  {isolation.workdir ? (
                    <div>
                      <dt>目录</dt>
                      <dd className="mono" title={isolation.workdir}>
                        {isolation.workdir}
                      </dd>
                    </div>
                  ) : null}
                </dl>
                {isolation.degraded ? (
                  <div className="td-warning-note" role="status">
                    <AlertTriangle size={14} />
                    <div className="td-warning-note-main">
                      <div className="td-warning-note-title">
                        已从 {isolation.requested_mode || task.isolation} 降级为{" "}
                        {isolation.mode || "未知模式"}
                      </div>
                      <div className="td-warning-note-body">
                        {isolation.degrade_reason ||
                          isolation.degrade_code ||
                          "降级原因未记录，请按实际模式评估风险。"}
                      </div>
                    </div>
                  </div>
                ) : null}
              </>
            ) : (
              <div className="td-isolation-declared">
                任务声明：<code>{task.isolation}</code>
                <div className="td-muted-note">
                  尚未执行或这是旧任务，没有实际隔离记录；以运行后的结果为准。
                </div>
              </div>
            )}
          </Panel>

          {processes ? (
            <Panel className="td-panel">
              <SectionHeader
                title={
                  <span className="td-section-title">
                    <Cpu size={15} /> 进程与端口
                  </span>
                }
                subtitle="Agent 运行期间启动的本机进程记录"
                actions={
                  <Badge tone="outline">
                    {startedProcesses.length + reapedProcesses.length} 条
                  </Badge>
                }
              />
              {startedProcesses.length ? (
                <div className="td-process-group">
                  <div className="td-process-label">启动记录</div>
                  <ProcessList entries={startedProcesses} />
                </div>
              ) : null}
              {reapedProcesses.length ? (
                <div className="td-process-group">
                  <div className="td-process-label">回收记录</div>
                  <ProcessList entries={reapedProcesses} />
                </div>
              ) : null}
              {!startedProcesses.length && !reapedProcesses.length ? (
                <div className="td-inline-empty">没有记录到进程。</div>
              ) : null}
              {processWarnings.length ? (
                <div className="td-warning-note is-process" role="alert">
                  <AlertTriangle size={14} />
                  <div className="td-warning-note-main">
                    <div className="td-warning-note-title">
                      未能确认归属，未自动回收，请手动检查端口
                    </div>
                    <ul className="td-process-warnings">
                      {processWarnings.map((warning, index) => (
                        <li key={index}>{String(warning)}</li>
                      ))}
                    </ul>
                  </div>
                </div>
              ) : null}
            </Panel>
          ) : null}

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <ListChecks size={15} /> 验收条件
                </span>
              }
              subtitle={task.acceptance_ref ? `引用：${task.acceptance_ref}` : "全部条件满足后任务才算成功"}
              actions={
                <div className="td-acceptance-actions">
                  <Badge tone="outline">{rawAcceptance.length} 条</Badge>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => openCorrection()}
                    title="只修改验收条件，不会伪造通过"
                  >
                    <Wrench size={13} /> 修正验收路径
                  </Button>
                </div>
              }
            />
            {acceptanceResult ? (
              <div
                className={`td-acceptance-result ${acceptanceResult.ok ? "is-ok" : "is-failed"}`}
              >
                {acceptanceResult.ok ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
                <span>
                  {String(
                    acceptanceResult.summary ||
                      (acceptanceResult.ok ? "验收通过" : "验收未通过"),
                  )}
                </span>
              </div>
            ) : null}
            {rawAcceptance.length ? (
              <ul className="td-acceptance">
                {rawAcceptance.map((item, index) => {
                  const condition = describeAcceptance(item);
                  return (
                    <li key={index} className="td-acceptance-item">
                      <CheckCircle2 size={14} className="td-acceptance-icon" />
                      <div className="td-acceptance-main">
                        <div className="td-acceptance-label">{condition.label}</div>
                        {condition.details.length ? (
                          <div className="td-acceptance-details">
                            {condition.details.map((detail) => (
                              <span key={detail.label} className="td-acceptance-detail">
                                <span>{detail.label}</span>
                                <code>{detail.value}</code>
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <div className="td-inline-empty">使用任务类型的默认验收规则。</div>
            )}
            {diagnostics.length ? (
              <div className="td-acceptance-diagnostics">
                <div className="td-acceptance-diagnostics-title">
                  <AlertTriangle size={13} /> 失败项诊断（候选仅提示，不会自动放行）
                </div>
                {diagnostics.map((check, index) => {
                  const candidates = checkCandidates(check);
                  const suggestion = checkSuggestion(check);
                  const hint = typeof check.hint === "string" ? check.hint : "";
                  return (
                    <div key={`${check.type || "check"}-${index}`} className="td-acceptance-diagnostic">
                      <div className="td-acceptance-diagnostic-head">
                        <span className="td-acceptance-diagnostic-detail">
                          {check.detail || `${check.type || "验收条件"} 未通过`}
                        </span>
                        {suggestion ? (
                          <Button size="sm" variant="ghost" onClick={() => openCorrection(check)}>
                            <Wrench size={12} /> 采用建议
                          </Button>
                        ) : null}
                      </div>
                      {hint ? <div className="td-acceptance-hint">{hint}</div> : null}
                      {candidates.length ? (
                        <div className="td-acceptance-candidates">
                          <span className="td-acceptance-candidates-label">候选文件</span>
                          <div className="td-acceptance-candidate-list">
                            {candidates.map((candidate) => (
                              <code key={candidate} title={candidate}>
                                {candidate}
                              </code>
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ) : null}
          </Panel>

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <Timer size={15} /> 运行记录
                </span>
              }
              subtitle={runs.length ? `${runs.length} 次 Agent 调用` : "还没有运行记录"}
              actions={
                runs.length ? (
                  <Button size="sm" variant="ghost" onClick={() => onOpenLogs()}>
                    打开日志 <ArrowRight size={13} />
                  </Button>
                ) : null
              }
            />
            {runs.length ? (
              <div className="td-table-wrap">
                <table className="td-table">
                  <thead>
                    <tr>
                      <th>Agent</th>
                      <th>尝试</th>
                      <th>耗时</th>
                      <th>Token</th>
                      <th>状态</th>
                      <th>开始时间</th>
                      <th aria-label="操作" />
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => {
                      const agent = run.agent_label || run.agent_id || "agent";
                      return (
                        <tr key={run.id}>
                          <td>
                            <span className="td-run-agent">
                              <span
                                className="td-avatar"
                                style={{
                                  background: `${agentColor(agent)}1a`,
                                  color: agentColor(agent),
                                }}
                              >
                                {initials(agent)}
                              </span>
                              <span className="td-run-agent-text">
                                <span>{agent}</span>
                                <span className="td-run-role">
                                  {run.protocol || "cli"}
                                  {run.role ? ` · ${run.role}` : ""}
                                </span>
                              </span>
                            </span>
                          </td>
                          <td className="td-num">#{run.attempt || 1}</td>
                          <td className="td-num">
                            <span className="td-duration">
                              <Clock3 size={11} /> {runDuration(run) ? fmtDuration(runDuration(run)) : "—"}
                            </span>
                          </td>
                          <td className="td-num">{fmtInt(run.cost_tokens)}</td>
                          <td>
                            <RunStatusBadge status={run.status} />
                          </td>
                          <td className="td-muted-cell">{fmtDateTime(run.started_at)}</td>
                          <td className="td-cell-action">
                            <button
                              type="button"
                              className="td-link"
                              onClick={() => onOpenLogs(run.id)}
                              title={run.error || "查看这次运行的输出"}
                            >
                              日志
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                icon={<Timer size={17} />}
                title="还没有执行过"
                hint="点击右上角的运行按钮，或直接启动项目调度。"
              />
            )}
          </Panel>

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <Package size={15} /> 项目产物
                </span>
              }
              subtitle="Agent 写入的文件与结构化产出"
              actions={<Badge tone="outline">{artifacts.length}</Badge>}
            />
            {artifacts.length ? (
              <>
                <ul className="td-artifacts">
                {visibleArtifacts.map((artifact) => {
                  const path = artifact.rel_path || artifact.abs_path || "";
                  return (
                    <li key={artifact.id} className="td-artifact">
                      <span className="td-artifact-icon">
                        <ArtifactIcon path={path} />
                      </span>
                      <span className="td-artifact-main">
                        <span className="td-artifact-name">{fileName(path)}</span>
                        <span className="td-artifact-path" title={path}>
                          {dirName(path) || "."}
                        </span>
                      </span>
                      <span className="td-artifact-meta">
                        {artifact.role ? <Badge tone="outline">{artifact.role}</Badge> : null}
                        <span className="td-artifact-size">{artifactMeta(artifact)}</span>
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => void openArtifact(artifact)}
                        title={artifact.abs_path || artifact.rel_path || "打开"}
                      >
                        <FolderOpen size={13} /> 打开
                      </Button>
                    </li>
                  );
                })}
                </ul>
                {artifacts.length > 12 ? (
                  <button
                    type="button"
                    className="td-list-more"
                    onClick={() => setShowAllArtifacts((value) => !value)}
                  >
                    {showAllArtifacts
                      ? "收起产物列表"
                      : `显示全部 ${artifacts.length} 个产物`}
                  </button>
                ) : null}
              </>
            ) : (
              <EmptyState
                icon={<Package size={17} />}
                title="还没有产物"
                hint="任务运行后，写出的文件会自动汇总到这里。"
              />
            )}
          </Panel>
        </div>

        <aside className="td-overview-side">
          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <Layers size={15} /> 交接说明
                </span>
              }
              subtitle={handoff?.created_at ? fmtDateTime(String(handoff.created_at)) : "Agent 留给下一次执行"}
            />
            {handoff ? (
              <div className="td-handoff">
                {handoffSections.map((section) => {
                  const key = [section.key, ...(section.aliases ?? [])].find(
                    (candidate) => handoff[candidate] !== undefined && handoff[candidate] !== null,
                  );
                  if (!key) return null;
                  const value = handoff[key];
                  if (value === "" || (Array.isArray(value) && value.length === 0)) return null;
                  return (
                    <div key={section.key} className="td-handoff-item">
                      <div className="td-handoff-label">{section.label}</div>
                      <HandoffValue value={value} />
                    </div>
                  );
                })}
                {handoffSections.every((section) => {
                  const key = [section.key, ...(section.aliases ?? [])].find(
                    (candidate) => handoff[candidate] !== undefined && handoff[candidate] !== null,
                  );
                  return !key;
                }) ? (
                  <div className="td-inline-empty">交接记录为空。</div>
                ) : null}
              </div>
            ) : (
              <EmptyState
                icon={<Layers size={16} />}
                title="还没有交接说明"
                hint="任务完成后，Agent 会在这里留下已完成、遗留问题与下一步。"
              />
            )}
            <div className="td-resume-note">
              <RotateCcw size={12} />
              <span>
                重试为文件级续跑：新 Agent 会话 + 恢复上次 write_scope 内产物，不是同一 ACP 会话
              </span>
            </div>
          </Panel>

          {costInfo || runs.length ? (
            <Panel className="td-panel">
              <SectionHeader
                title={
                  <span className="td-section-title">
                    <CircleDollarSign size={15} /> 成本与用量
                  </span>
                }
                subtitle={costInfo ? "成本来源以 Agent 上报为准" : "历史任务没有成本来源记录"}
                actions={
                  costInfo ? (
                    <Badge
                      tone={
                        String(costInfo.source) === "reported"
                          ? "success"
                          : String(costInfo.source) === "unreported"
                            ? "warning"
                            : "outline"
                      }
                    >
                      {costSourceLabel(costInfo)}
                    </Badge>
                  ) : (
                    <Badge tone="outline">来源未知</Badge>
                  )
                }
              />
              {costInfo ? (
                <>
                  <dl className="td-kv">
                    <div>
                      <dt>金额</dt>
                      <dd>{costAmountText(costInfo)}</dd>
                    </div>
                    <div>
                      <dt>Token</dt>
                      <dd>
                        {Number.isFinite(Number(costInfo.tokens))
                          ? fmtInt(Number(costInfo.tokens))
                          : "—"}
                      </dd>
                    </div>
                  </dl>
                  {String(costInfo.source) === "unreported" ? (
                    <div className="td-cost-note is-warning">
                      未上报（Agent 协议未提供成本数据）。0 不代表免费。
                    </div>
                  ) : null}
                  {String(costInfo.source) === "reported" &&
                  Number(costInfo.amount) === 0 &&
                  Number(costInfo.tokens) > 0 ? (
                    <div className="td-cost-note is-warning">
                      费用未提供：Agent 只上报了 Token，不能按免费计算。
                    </div>
                  ) : null}
                  {String(costInfo.source) === "estimated" ? (
                    <div className="td-cost-note">
                      金额为估算值（≈），不是 Agent 上报的真实账单。
                    </div>
                  ) : null}
                </>
              ) : (
                <>
                  <dl className="td-kv">
                    <div>
                      <dt>来源</dt>
                      <dd>历史数据（来源未知）</dd>
                    </div>
                    <div>
                      <dt>运行汇总</dt>
                      <dd>
                        {runs.length} 次 · {fmtInt(totalTokens(runs))} tokens
                        {runCost > 0 ? ` · 金额 ${runCost.toFixed(2)}` : ""}
                      </dd>
                    </div>
                  </dl>
                  <div className="td-cost-note">
                    任务没有 result.cost；0 不代表免费，成本来源无法确认。
                  </div>
                </>
              )}
            </Panel>
          ) : null}

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <History size={15} /> 最近状态
                </span>
              }
              subtitle="任务状态变化"
            />
            {history.length ? (
              <ol className="td-history">
                {history.map((entry, index) => (
                  <li key={`${entry.at}-${entry.status}-${index}`} className="td-history-item">
                    <span className={`td-history-dot is-${entry.status || "default"}`} />
                    <div className="td-history-main">
                      <div className="td-history-title">
                        <StatusBadge status={entry.status as Task["status"]} dot={false} />
                        <span className="td-history-time">{fmtDateTime(entry.at)}</span>
                      </div>
                      {entry.reason ? <div className="td-history-reason">{entry.reason}</div> : null}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="td-inline-empty">暂无状态记录。</div>
            )}
          </Panel>

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <Info size={15} /> 任务信息
                </span>
              }
              subtitle="用于排查与审计"
            />
            <dl className="td-kv">
              <div>
                <dt>任务 ID</dt>
                <dd className="mono" title={task.id}>
                  {task.id}
                </dd>
              </div>
              <div>
                <dt>创建</dt>
                <dd>{fmtDateTime(task.created_at)}</dd>
              </div>
              <div>
                <dt>更新</dt>
                <dd>{fmtDateTime(task.updated_at)}</dd>
              </div>
              <div>
                <dt>工作目录</dt>
                <dd className="mono" title={task.workdir || undefined}>
                  {task.workdir || "运行后生成"}
                </dd>
              </div>
            </dl>
            <div className="td-kv-actions">
              {changes.length ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => onTab("changes")}
                >
                  <FileDiff size={13} /> 查看改动
                </Button>
              ) : null}
              <Button size="sm" variant="ghost" onClick={() => onTab("context")}>
                查看上下文
              </Button>
            </div>
            {task.workdir ? (
              <Button
                size="sm"
                variant="ghost"
                className="td-kv-action"
                onClick={async () => {
                  const result = await openLocalPath(task.workdir ?? "");
                  if (result === "desktop") toast.success("已打开工作目录");
                  else if (result === "copied") toast.info("已复制目录路径");
                  else toast.error("无法打开工作目录");
                }}
              >
                <ExternalLink size={13} /> 打开工作目录
              </Button>
            ) : null}
          </Panel>
        </aside>
      </div>

      <Dialog
        open={correctionOpen}
        onClose={() => setCorrectionOpen(false)}
        title="修正验收路径"
        description="只修改任务的验收条件，不会自动判定通过；保存后请重新运行或重新验收。"
        width={720}
        footer={
          <>
            <Button onClick={() => setCorrectionOpen(false)}>取消</Button>
            <Button variant="primary" loading={correctionSaving} onClick={() => void saveAcceptance()}>
              保存
            </Button>
          </>
        }
      >
        {suggestionChecks.length ? (
          <div className="td-correction-suggestions">
            <div className="td-correction-suggestions-label">根据失败项生成的建议（只替换对应条件）</div>
            {suggestionChecks.map((check, index) => {
              const suggestion = checkSuggestion(check);
              if (!suggestion) return null;
              return (
                <button
                  key={`${check.type || "check"}-${index}`}
                  type="button"
                  className="td-correction-suggestion"
                  onClick={() => adoptSuggestion(check)}
                >
                  <Wrench size={13} />
                  <span>
                    采用建议：{String(suggestion.type || "条件")}
                    {suggestion.pattern ? ` · ${String(suggestion.pattern)}` : ""}
                  </span>
                </button>
              );
            })}
          </div>
        ) : null}
        <Field
          label="验收条件 JSON"
          hint="必须是对象数组；保存前会再次校验，格式错误不会提交。"
          htmlFor="td-acceptance-json"
        >
          <Textarea
            id="td-acceptance-json"
            className="td-acceptance-json"
            rows={14}
            value={correctionText}
            onChange={(event) => {
              setCorrectionText(event.target.value);
              setCorrectionError("");
            }}
            spellCheck={false}
          />
        </Field>
        {correctionError ? (
          <div className="td-correction-error" role="alert">
            <AlertTriangle size={13} />
            <span>{correctionError}</span>
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}
