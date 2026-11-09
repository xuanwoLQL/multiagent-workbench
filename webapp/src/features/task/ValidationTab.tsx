import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  FlaskConical,
  MinusCircle,
  Play,
  RefreshCw,
  ShieldCheck,
  Timer,
  XCircle,
} from "lucide-react";
import { ApiError, api } from "../../api/client";
import { Badge, Button, EmptyState, Panel, SectionHeader, Select } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { fmtDateTime, fmtDuration, shortPath } from "../../lib/format";
import { copyText, useNow } from "./helpers";

const CHECK_ORDER = ["typecheck", "test", "build", "lint"] as const;
type CheckId = (typeof CHECK_ORDER)[number];

const CHECK_META: Record<CheckId, { label: string; hint: string }> = {
  typecheck: { label: "类型检查", hint: "tsc / mypy / pyright / cargo check" },
  test: { label: "测试", hint: "pytest / vitest / cargo test / go test" },
  build: { label: "构建", hint: "build / compile / package" },
  lint: { label: "静态检查", hint: "eslint / ruff / clippy / go vet" },
};

const TIMEOUT_OPTIONS = [120, 300, 600] as const;

interface CommandSpec {
  argv?: string[];
  label?: string;
  source?: string;
  display?: string;
}

interface BlockedCheck {
  check?: string;
  source?: string;
  reason?: string;
  script?: string;
  target?: string;
  command?: string;
}

export interface ValidationProfile {
  root?: string;
  primary_language?: string | null;
  languages?: string[];
  package_managers?: string[];
  markers?: string[];
  commands?: Record<string, CommandSpec>;
  recommended?: string[];
  blocked?: BlockedCheck[];
  default_timeout_sec?: number;
  files_scanned?: number;
}

export interface ValidationCheck {
  id: string;
  label?: string;
  command?: string;
  ok?: boolean;
  exit_code?: number | null;
  duration_ms?: number;
  output?: string;
  skipped?: boolean;
  timeout?: boolean;
  cancelled?: boolean;
  truncated?: boolean;
}

interface ValidationSummary {
  total?: number;
  passed?: number;
  failed?: number;
  skipped?: number;
  timeout?: number;
  cancelled?: number;
  duration_ms?: number;
}

export interface ValidationReport {
  report_id?: string;
  task_id?: string;
  ok?: boolean;
  checks?: ValidationCheck[];
  summary?: ValidationSummary;
  empty?: boolean;
  message?: string;
  started_at?: string;
  finished_at?: string;
  generated_at?: string;
  timeout_sec?: number;
  mode?: string;
  root?: string;
  requested?: string[];
  profile?: ValidationProfile;
}

type CheckStateKey = "passed" | "failed" | "timeout" | "cancelled" | "skipped";

interface CheckState {
  key: CheckStateKey;
  label: string;
  tone: "success" | "danger" | "warning" | "default";
  icon: ReactNode;
}

function checkState(check: ValidationCheck): CheckState {
  if (check.timeout) {
    return { key: "timeout", label: "超时", tone: "danger", icon: <Timer size={14} /> };
  }
  if (check.cancelled) {
    return {
      key: "cancelled",
      label: "已取消",
      tone: "warning",
      icon: <MinusCircle size={14} />,
    };
  }
  if (check.skipped) {
    return { key: "skipped", label: "跳过", tone: "default", icon: <MinusCircle size={14} /> };
  }
  if (check.ok) {
    return { key: "passed", label: "通过", tone: "success", icon: <CheckCircle2 size={14} /> };
  }
  return { key: "failed", label: "失败", tone: "danger", icon: <XCircle size={14} /> };
}

function fmtCheckDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
  return fmtDuration(ms);
}

function exitLabel(check: ValidationCheck): string {
  if (check.skipped) return "未执行";
  if (check.exit_code === null || check.exit_code === undefined) return "exit —";
  return `exit ${check.exit_code}`;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function pickProfile(raw: unknown): ValidationProfile | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const nested = record.profile;
  const source =
    nested && typeof nested === "object" ? (nested as Record<string, unknown>) : record;
  if (!("commands" in source) && !("languages" in source) && !("package_managers" in source)) {
    return null;
  }
  return source as ValidationProfile;
}

function pickReport(raw: unknown): ValidationReport | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const nested = record.report;
  return (nested && typeof nested === "object" ? nested : record) as ValidationReport;
}

function ChipList({
  items,
  empty,
  max = 12,
}: {
  items: string[];
  empty: string;
  max?: number;
}) {
  if (!items.length) return <span className="td-val-empty-chip">{empty}</span>;
  const shown = items.slice(0, max);
  const rest = items.length - shown.length;
  return (
    <div className="td-val-chips">
      {shown.map((item) => (
        <span key={item} className="td-val-chip mono" title={item}>
          {item}
        </span>
      ))}
      {rest > 0 ? (
        <span className="td-val-chip" title={items.slice(max).join(", ")}>
          +{rest}
        </span>
      ) : null}
    </div>
  );
}

function CheckRow({
  check,
  expanded,
  onToggle,
}: {
  check: ValidationCheck;
  expanded: boolean;
  onToggle: () => void;
}) {
  const toast = useToast();
  const state = checkState(check);
  const output = String(check.output ?? "");
  const title = CHECK_META[check.id as CheckId]?.label || check.label || check.id;

  const copy = async (text: string, label: string) => {
    if (!text) {
      toast.error("没有可复制的内容");
      return;
    }
    const ok = await copyText(text);
    if (ok) toast.success(`${label}已复制`);
    else toast.error("复制失败");
  };

  return (
    <div className={clsx("td-val-check", `is-${state.key}`)}>
      <button
        type="button"
        className="td-val-check-head"
        aria-expanded={expanded}
        onClick={onToggle}
      >
        <span className="td-val-check-status">{state.icon}</span>
        <span className="td-val-check-main">
          <span className="td-val-check-title">
            <span className="td-val-check-name">{title}</span>
            <Badge tone={state.tone}>{state.label}</Badge>
            <span className="td-val-check-id mono">{check.id}</span>
          </span>
          <span className="td-val-check-cmd mono" title={check.command || undefined}>
            {check.command || "—"}
          </span>
        </span>
        <span className="td-val-check-facts">
          <span className="td-val-check-duration">{fmtCheckDuration(check.duration_ms)}</span>
          <span className="td-val-check-exit mono">{exitLabel(check)}</span>
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>

      {expanded ? (
        <div className="td-val-output-wrap">
          <div className="td-val-output-bar">
            <span className="td-val-output-note">
              {check.truncated ? "输出已截断（上限 200 KB）" : "输出"}
            </span>
            <div className="td-val-output-actions">
              {check.command ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void copy(String(check.command), "命令")}
                >
                  <Copy size={12} /> 复制命令
                </Button>
              ) : null}
              <Button size="sm" variant="ghost" onClick={() => void copy(output, "输出")}>
                <Copy size={12} /> 复制输出
              </Button>
            </div>
          </div>
          {output.trim() ? (
            <pre className="td-val-output">{output}</pre>
          ) : (
            <div className="td-val-output-empty">（无输出）</div>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function ValidationTab({ taskId, projectId }: { taskId: string; projectId: string }) {
  const toast = useToast();
  const client = useQueryClient();
  const [selected, setSelected] = useState<CheckId[]>([]);
  const [timeoutSec, setTimeoutSec] = useState<number>(600);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<{ status: number | null; message: string } | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const initialized = useRef(false);
  const runStartedAt = useRef<number | null>(null);
  const expandedReport = useRef<string | null>(null);
  const now = useNow(running);

  const inspectQuery = useQuery({
    queryKey: ["task-inspect", projectId],
    queryFn: async () => pickProfile(await api.get<unknown>(`/api/projects/${projectId}/inspect`)),
    enabled: Boolean(projectId),
    staleTime: 60_000,
    retry: false,
  });

  const reportQuery = useQuery({
    queryKey: ["task-validation", taskId],
    queryFn: async (): Promise<ValidationReport | null> => {
      try {
        return pickReport(await api.get<unknown>(`/api/tasks/${taskId}/validation`));
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    enabled: Boolean(taskId),
    retry: false,
    staleTime: 5_000,
  });

  const report = reportQuery.data ?? null;
  const profile = inspectQuery.data ?? report?.profile ?? null;
  const commands = profile?.commands ?? {};
  const recommended = useMemo(() => new Set(profile?.recommended ?? []), [profile]);

  useEffect(() => {
    if (initialized.current || !profile) return;
    const detected = CHECK_ORDER.filter((id) => Boolean(profile.commands?.[id]));
    const rec = (profile.recommended ?? []).filter((id): id is CheckId =>
      (CHECK_ORDER as readonly string[]).includes(id),
    );
    setSelected(rec.length ? rec : detected);
    const defaultTimeout = Number(profile.default_timeout_sec);
    if ((TIMEOUT_OPTIONS as readonly number[]).includes(defaultTimeout)) {
      setTimeoutSec(defaultTimeout);
    }
    initialized.current = true;
  }, [profile]);

  useEffect(() => {
    const current = reportQuery.data;
    if (!current) return;
    const identity = current.report_id || current.finished_at || current.generated_at || "";
    if (!identity || expandedReport.current === identity) return;
    expandedReport.current = identity;
    const failures = (current.checks ?? [])
      .filter((check) => !check.ok && !check.skipped)
      .map((check) => check.id);
    if (failures.length) {
      setExpanded((prev) => new Set([...prev, ...failures]));
    }
  }, [reportQuery.data]);

  const toggleCheck = (id: CheckId) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  };

  const selectAll = () => {
    setSelected(CHECK_ORDER.filter((id) => Boolean(commands[id])));
  };

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const run = async () => {
    const checks = CHECK_ORDER.filter((id) => selected.includes(id));
    if (!checks.length) {
      toast.error("请至少选择一个检查");
      return;
    }
    runStartedAt.current = Date.now();
    setRunning(true);
    setRunError(null);
    try {
      const next = pickReport(
        await api.post<unknown>(`/api/tasks/${taskId}/validate`, {
          checks,
          timeout_sec: timeoutSec,
        }),
      );
      if (!next) throw new Error("服务端返回了无法识别的验证报告");
      client.setQueryData(["task-validation", taskId], next);
      if (next.ok) {
        toast.success("验证通过", next.message);
      } else if (next.empty) {
        toast.info(next.message || "没有可运行的检查");
      } else {
        toast.error("验证未通过", next.message);
      }
    } catch (error) {
      const status = error instanceof ApiError ? error.status : null;
      const message = errorText(error);
      setRunError({ status, message });
      toast.error("验证请求失败", message);
    } finally {
      setRunning(false);
      runStartedAt.current = null;
    }
  };

  const checks = report?.checks ?? [];
  const summary = report?.summary ?? {};
  const passed = summary.passed ?? checks.filter((item) => item.ok && !item.skipped).length;
  const failed = summary.failed ?? checks.filter(
    (item) => !item.ok && !item.skipped && !item.timeout && !item.cancelled,
  ).length;
  const skipped = summary.skipped ?? checks.filter((item) => item.skipped).length;
  const timedOut = summary.timeout ?? checks.filter((item) => item.timeout).length;
  const total = summary.total ?? checks.length;
  const reportTime = report?.finished_at || report?.generated_at || report?.started_at || "";
  const overall = report
    ? report.ok
      ? { tone: "success" as const, label: "验证通过" }
      : report.empty || !checks.length
        ? { tone: "warning" as const, label: "无可运行检查" }
        : { tone: "danger" as const, label: "验证未通过" }
    : null;
  const elapsedMs =
    running && runStartedAt.current ? Math.max(0, now - runStartedAt.current) : 0;

  return (
    <div className="td-val">
      <div className="td-val-top">
        <Panel className="td-panel td-val-card">
          <SectionHeader
            title={
              <span className="td-section-title">
                <ShieldCheck size={15} /> 项目栈
              </span>
            }
            subtitle={
              profile?.root
                ? `检测目录：${shortPath(profile.root, 4)}`
                : "只读扫描任务工作副本，用于确定可运行的检查"
            }
            actions={
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void inspectQuery.refetch()}
                loading={inspectQuery.isFetching}
              >
                <RefreshCw size={12} /> 重新检测
              </Button>
            }
          />

          {inspectQuery.isPending ? (
            <div className="td-val-skeletons">
              <span className="skeleton td-val-skel" />
              <span className="skeleton td-val-skel" />
              <span className="skeleton td-val-skel is-short" />
            </div>
          ) : inspectQuery.isError ? (
            <div className="td-val-inline-error" role="alert">
              <AlertTriangle size={14} />
              <div>
                <div className="td-val-inline-error-title">无法检测项目栈</div>
                <div className="td-val-inline-error-body">{errorText(inspectQuery.error)}</div>
              </div>
            </div>
          ) : profile ? (
            <div className="td-val-profile">
              <div className="td-val-meta">
                <span>
                  主语言{" "}
                  <strong>{profile.primary_language || "未识别"}</strong>
                </span>
                <span>
                  扫描文件 <strong>{profile.files_scanned ?? "—"}</strong>
                </span>
                {profile.root ? (
                  <span className="td-val-root mono" title={profile.root}>
                    {shortPath(profile.root, 3)}
                  </span>
                ) : null}
              </div>

              <div className="td-val-group">
                <div className="td-val-group-label">语言</div>
                <ChipList items={profile.languages ?? []} empty="未检测到语言标记" />
              </div>
              <div className="td-val-group">
                <div className="td-val-group-label">包管理器</div>
                <ChipList items={profile.package_managers ?? []} empty="未检测到" />
              </div>
              <div className="td-val-group">
                <div className="td-val-group-label">标记文件</div>
                <ChipList items={profile.markers ?? []} empty="未检测到" max={10} />
              </div>
              <div className="td-val-group">
                <div className="td-val-group-label">推荐检查</div>
                {profile.recommended?.length ? (
                  <div className="td-val-chips">
                    {profile.recommended.map((id) => (
                      <Badge key={id} tone="accent">
                        {CHECK_META[id as CheckId]?.label ?? id}
                        <span className="td-val-rec-id mono">{id}</span>
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <span className="td-val-empty-chip">
                    没有检测到可自动运行的检查命令
                  </span>
                )}
              </div>

              {profile.blocked?.length ? (
                <div className="td-val-blocked" role="note">
                  <AlertTriangle size={14} />
                  <div>
                    <div className="td-val-blocked-title">
                      {profile.blocked.length} 项脚本包含安装命令，已拒绝执行
                    </div>
                    <ul className="td-val-blocked-list">
                      {profile.blocked.map((item, index) => (
                        <li key={`${item.check ?? "check"}-${index}`}>
                          <span className="mono">{item.check ?? item.source ?? "script"}</span>
                          <span>{item.reason || "包含依赖安装命令"}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="td-val-inline-empty">
              没有拿到项目检测结果。刷新或重新检测后可查看语言、包管理器与可用命令。
            </div>
          )}
        </Panel>

        <Panel className="td-panel td-val-card">
          <SectionHeader
            title={
              <span className="td-section-title">
                <FlaskConical size={15} /> 运行检查
              </span>
            }
            subtitle="在任务工作副本中执行，不会安装依赖或修改工作区"
            actions={
              <Button
                size="sm"
                variant="ghost"
                onClick={selectAll}
                disabled={running || !CHECK_ORDER.some((id) => Boolean(commands[id]))}
              >
                全选可用
              </Button>
            }
          />

          <div className="td-val-options" role="group" aria-label="选择要运行的检查">
            {CHECK_ORDER.map((id) => {
              const spec = commands[id];
              const active = selected.includes(id);
              const isRecommended = recommended.has(id);
              return (
                <button
                  key={id}
                  type="button"
                  className={clsx(
                    "td-val-option",
                    active && "is-active",
                    !spec && "is-missing",
                  )}
                  aria-pressed={active}
                  disabled={running}
                  title={CHECK_META[id].hint}
                  onClick={() => toggleCheck(id)}
                >
                  <span className="td-val-option-box" aria-hidden="true">
                    {active ? <Check size={11} /> : null}
                  </span>
                  <span className="td-val-option-main">
                    <span className="td-val-option-title">
                      {CHECK_META[id].label}
                      <span className="td-val-option-id mono">{id}</span>
                      {isRecommended ? <Badge tone="accent">推荐</Badge> : null}
                    </span>
                    <span
                      className="td-val-option-cmd mono"
                      title={spec?.display || spec?.label || undefined}
                    >
                      {spec?.display || spec?.label || "未检测到命令"}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>

          <div className="td-val-runbar">
            <label className="td-val-timeout">
              <span>
                <Timer size={13} /> 超时
              </span>
              <Select
                value={String(timeoutSec)}
                disabled={running}
                aria-label="单次检查超时时间"
                onChange={(event) => setTimeoutSec(Number(event.target.value))}
              >
                {TIMEOUT_OPTIONS.map((seconds) => (
                  <option key={seconds} value={seconds}>
                    {seconds} 秒
                  </option>
                ))}
              </Select>
            </label>
            <span className="td-val-selected">
              已选 <strong>{selected.length}</strong> 项
            </span>
            <span className="td-val-spacer" />
            <Button
              variant="primary"
              onClick={() => void run()}
              loading={running}
              disabled={!selected.length}
            >
              {running ? null : <Play size={14} />}
              {running ? `运行中 ${fmtCheckDuration(elapsedMs)}` : "运行检查"}
            </Button>
          </div>

          {running ? (
            <div className="td-val-running" role="status">
              正在执行 {selected.length} 项检查，超时上限 {timeoutSec} 秒。请保持页面打开。
            </div>
          ) : null}

          {runError ? (
            <div className="td-alert td-val-run-error" role="alert">
              <span className="td-alert-icon">
                <AlertTriangle size={16} />
              </span>
              <div className="td-alert-main">
                <div className="td-alert-title">
                  {runError.status === 409
                    ? "当前任务还没有可验证的工作副本"
                    : runError.status === 400
                      ? "验证请求被拒绝"
                      : "验证请求失败"}
                </div>
                <div className="td-alert-body">{runError.message}</div>
                {runError.status === 409 ? (
                  <div className="td-val-run-hint">
                    先运行一次任务生成隔离副本，或把任务隔离方式改为 shared 后再验证。
                  </div>
                ) : null}
              </div>
              <div className="td-alert-actions">
                <Button size="sm" variant="ghost" onClick={() => setRunError(null)}>
                  关闭
                </Button>
              </div>
            </div>
          ) : null}
        </Panel>
      </div>

      <Panel className="td-panel td-val-card td-val-report">
        <SectionHeader
          title={
            <span className="td-section-title">
              <ShieldCheck size={15} /> 最近一次验证
            </span>
          }
          subtitle="验证报告保存在本机，用于确认改动没有破坏项目"
          actions={
            <div className="td-val-report-actions">
              {report ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={async () => {
                    const ok = await copyText(JSON.stringify(report, null, 2));
                    if (ok) toast.success("验证报告已复制");
                    else toast.error("复制失败");
                  }}
                >
                  <Copy size={12} /> 复制报告
                </Button>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void reportQuery.refetch()}
                loading={reportQuery.isFetching}
              >
                <RefreshCw size={12} /> 刷新
              </Button>
            </div>
          }
        />

        {reportQuery.isPending ? (
          <div className="td-val-skeletons">
            <span className="skeleton td-val-skel" />
            <span className="skeleton td-val-skel" />
            <span className="skeleton td-val-skel is-short" />
          </div>
        ) : reportQuery.isError ? (
          <div className="td-val-inline-error" role="alert">
            <AlertTriangle size={14} />
            <div>
              <div className="td-val-inline-error-title">无法加载最近一次验证</div>
              <div className="td-val-inline-error-body">{errorText(reportQuery.error)}</div>
            </div>
            <Button size="sm" variant="ghost" onClick={() => void reportQuery.refetch()}>
              重试
            </Button>
          </div>
        ) : report ? (
          <div className="td-val-report-body">
            <div className="td-val-report-head">
              {overall ? <Badge tone={overall.tone}>{overall.label}</Badge> : null}
              <span className="td-val-report-message">{report.message || "—"}</span>
              <span className="td-val-report-meta">
                <span>{reportTime ? fmtDateTime(reportTime) : "—"}</span>
                <span>{fmtDuration(summary.duration_ms)}</span>
                <span>上限 {report.timeout_sec ?? "—"} 秒</span>
                {report.mode ? <span>{report.mode}</span> : null}
              </span>
            </div>

            <div className="td-val-summary">
              <span className="td-val-summary-item">
                通过 <strong>{passed}</strong>
              </span>
              <span className="td-val-summary-item is-danger">
                失败 <strong>{failed}</strong>
              </span>
              <span className="td-val-summary-item is-warning">
                跳过 <strong>{skipped}</strong>
              </span>
              <span className="td-val-summary-item is-danger">
                超时 <strong>{timedOut}</strong>
              </span>
              <span className="td-val-summary-item">
                共 <strong>{total}</strong> 项
              </span>
            </div>

            {report.root ? (
              <div className="td-val-report-root mono" title={report.root}>
                工作目录：{shortPath(report.root, 4)}
              </div>
            ) : null}

            {checks.length ? (
              <div className="td-val-check-list">
                {checks.map((check) => (
                  <CheckRow
                    key={check.id}
                    check={check}
                    expanded={expanded.has(check.id)}
                    onToggle={() => toggleExpanded(check.id)}
                  />
                ))}
              </div>
            ) : (
              <div className="td-val-inline-empty">
                报告里没有检查项。{report.message || "请确认项目包含可运行的检查命令。"}
              </div>
            )}
          </div>
        ) : (
          <EmptyState
            icon={<FlaskConical size={20} />}
            title="还没有验证报告"
            hint={
              <>
                验证会在任务工作副本中运行项目自带的 typecheck / test / build / lint，确认改动没有破坏工程。
                它和「验收条件」不同：验收判断任务是否达成目标（exit_zero、file_modified 等），验证判断项目是否仍然可构建、可测试。
              </>
            }
            action={
              <Button
                variant="primary"
                onClick={() => void run()}
                loading={running}
                disabled={!selected.length}
              >
                <Play size={14} /> 运行检查
              </Button>
            }
          />
        )}
      </Panel>
    </div>
  );
}
