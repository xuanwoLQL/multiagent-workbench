import { useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  Cpu,
  Gauge,
  PlugZap,
  RefreshCw,
  ShieldCheck,
  Terminal,
  Wallet,
  Wrench,
} from "lucide-react";
import { api } from "../../api/client";
import { useAgents } from "../../app/queries";
import { fmtDuration, fmtInt, initials } from "../../lib/format";
import { Badge, Button, EmptyState, Panel } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import "./agents.css";

interface Quota {
  max_invocations?: number;
  max_seconds?: number;
  max_cost?: number;
  [key: string]: unknown;
}

interface AgentUsageNumbers {
  invocations?: number;
  seconds?: number;
  cost?: number;
  tokens?: number;
}

interface AgentDetail {
  id: string;
  label: string;
  protocol: string;
  enabled: boolean;
  command: string | null;
  args: string[];
  cwd?: string | null;
  roles: string[];
  strengths: string[];
  can_write: boolean;
  quota: Quota;
  usage: AgentUsageNumbers;
  model: string | null;
  error?: string;
}

interface RawAgentShape {
  id?: string;
  agent_id?: string;
  label?: string;
  protocol?: string;
  enabled?: boolean;
  command?: string | null;
  args?: string[];
  cwd?: string | null;
  roles?: string[];
  strengths?: string[];
  can_write?: boolean;
  invocations?: number;
  seconds?: number;
  cost?: number;
  tokens?: number;
  quota?: Quota;
  usage?: AgentUsageNumbers;
  model?: string | null;
  error?: string;
}

interface AgentConfigEntry {
  id?: string;
  model?: string | null;
  quota?: Quota;
}

interface AgentsConfig {
  defaults?: { quota?: Quota };
  agents?: AgentConfigEntry[];
}

interface ConfigFilesResponse {
  dir: string;
  files: Record<string, unknown | null>;
  secrets_redacted?: boolean;
}

interface CheckResponse {
  ok?: boolean;
  installed?: boolean;
  detail?: string;
}

type CheckEntry =
  | { state: "checking" }
  | { state: "ok" | "failed"; detail: string; checkedAt: number };

export function AgentsView() {
  const agentsQuery = useAgents();
  const configQuery = useQuery({
    queryKey: ["config-files"],
    queryFn: () => api.get<ConfigFilesResponse>("/api/config/files"),
    staleTime: 30_000,
    retry: false,
  });
  const toast = useToast();
  const [checks, setChecks] = useState<Record<string, CheckEntry>>({});

  const config = useMemo(
    () => parseAgentsConfig(configQuery.data?.files?.["agents.json"]),
    [configQuery.data],
  );

  const agents = useMemo(() => {
    const raw = (agentsQuery.data ?? []) as unknown as RawAgentShape[];
    return raw.map((item) => normalizeAgent(item, config));
  }, [agentsQuery.data, config]);

  const readyCount = agents.filter(isReady).length;
  const totals = useMemo(() => {
    return agents.reduce(
      (result, agent) => ({
        invocations: result.invocations + numberValue(agent.usage.invocations),
        seconds: result.seconds + numberValue(agent.usage.seconds),
        tokens: result.tokens + numberValue(agent.usage.tokens),
        cost: result.cost + numberValue(agent.usage.cost),
      }),
      { invocations: 0, seconds: 0, tokens: 0, cost: 0 },
    );
  }, [agents]);

  const refresh = async () => {
    try {
      const [agentResult, configResult] = await Promise.all([
        agentsQuery.refetch(),
        configQuery.refetch(),
      ]);
      if (agentResult.error) throw agentResult.error;
      if (configResult.error) throw configResult.error;
      toast.success("Agent 状态已刷新");
    } catch (error) {
      toast.error("刷新 Agent 失败", errorMessage(error));
    }
  };

  const checkAgent = async (agent: AgentDetail) => {
    if (!agent.id || checks[agent.id]?.state === "checking") return;
    setChecks((previous) => ({ ...previous, [agent.id]: { state: "checking" } }));
    try {
      const result = await api.post<CheckResponse>(
        `/api/agents/${encodeURIComponent(agent.id)}/check`,
        {},
      );
      const detail =
        result.detail ||
        (result.ok
          ? result.installed === false
            ? "ACP 握手成功，但未检测到本地命令"
            : "ACP 握手成功"
          : "未返回失败详情");
      setChecks((previous) => ({
        ...previous,
        [agent.id]: {
          state: result.ok ? "ok" : "failed",
          detail,
          checkedAt: Date.now(),
        },
      }));
      if (result.ok) toast.success(`${agent.label} 连接正常`, detail);
      else toast.error(`${agent.label} 不可用`, detail);
    } catch (error) {
      const detail = errorMessage(error);
      setChecks((previous) => ({
        ...previous,
        [agent.id]: { state: "failed", detail, checkedAt: Date.now() },
      }));
      toast.error(`${agent.label} 检测失败`, detail);
    }
  };

  return (
    <div className="page agents-page">
      <div className="page-head">
        <div className="page-head-text">
          <div className="page-title">
            <h1>Agent 编队</h1>
            {agents.length ? (
              <Badge tone={readyCount === agents.length ? "success" : "warning"} dot>
                {readyCount} / {agents.length} 就绪
              </Badge>
            ) : null}
          </div>
          <div className="page-subtitle">
            查看每个执行端的协议、角色、模型控制方式、额度与运行记录用量；连接检测会执行一次 ACP 握手探测。
          </div>
        </div>
        <div className="page-actions">
          <Button onClick={() => void refresh()} loading={agentsQuery.isFetching}>
            <RefreshCw size={14} /> 刷新
          </Button>
        </div>
      </div>

      {agentsQuery.isError ? (
        <Panel>
          <EmptyState
            icon={<AlertTriangle size={18} />}
            title="无法加载 Agent 列表"
            hint={errorMessage(agentsQuery.error)}
            action={
              <Button onClick={() => void agentsQuery.refetch()}>
                <RefreshCw size={14} /> 重试
              </Button>
            }
          />
        </Panel>
      ) : agentsQuery.isLoading ? (
        <AgentsSkeleton />
      ) : agents.length === 0 ? (
        <Panel>
          <EmptyState
            icon={<Bot size={19} />}
            title="还没有可用的 Agent"
            hint="请在 设置 → Agent 配置 中维护 agents.json；保存后刷新此页即可重新读取。"
          />
        </Panel>
      ) : (
        <>
          <section className="agents-summary" aria-label="Agent 状态汇总">
            <SummaryItem
              icon={<ShieldCheck size={15} />}
              label="就绪"
              value={`${readyCount} / ${agents.length}`}
              hint={readyCount === agents.length ? "全部执行端可用" : "有执行端需要检查"}
              tone={readyCount === agents.length ? "success" : "warning"}
            />
            <SummaryItem
              icon={<Activity size={15} />}
              label="累计调用"
              value={fmtInt(totals.invocations)}
              hint="所有 Agent 的历史调用"
            />
            <SummaryItem
              icon={<Terminal size={15} />}
              label="累计耗时"
              value={formatSeconds(totals.seconds)}
              hint="执行端运行时长"
            />
            <SummaryItem
              icon={<Gauge size={15} />}
              label="Tokens"
              value={fmtInt(totals.tokens)}
              hint="运行记录汇总"
            />
            <SummaryItem
              icon={<Wallet size={15} />}
              label="成本"
              value={formatMoney(totals.cost)}
              hint="按运行记录累计"
            />
          </section>

          <div className="agent-grid">
            {agents.map((agent) => (
              <AgentCard
                key={agent.id}
                agent={agent}
                check={checks[agent.id]}
                onCheck={() => void checkAgent(agent)}
              />
            ))}
          </div>

          <div className="agents-note">
            <ShieldCheck size={13} />
            启用状态与额度来自 <code>agents.json</code>，工作台不会提供伪造的启停开关；如需调整请到「设置 → Agent 配置」。
          </div>
        </>
      )}
    </div>
  );
}

function AgentCard({
  agent,
  check,
  onCheck,
}: {
  agent: AgentDetail;
  check?: CheckEntry;
  onCheck: () => void;
}) {
  const ready = isReady(agent);
  const checking = check?.state === "checking";

  return (
    <article className="agent-card" aria-label={`${agent.label} Agent`}>
      <header className="agent-card-head">
        <div className={`agent-mark${ready ? "" : " is-muted"}`} aria-hidden="true">
          {initials(agent.label)}
        </div>
        <div className="agent-identity">
          <div className="agent-name-line">
            <h2>{agent.label}</h2>
            <span className="agent-id mono">{agent.id}</span>
          </div>
          <div className="agent-protocol-line">
            <Badge tone="outline">{agent.protocol || "未声明协议"}</Badge>
            <span className="agent-model" title={modelControlTitle(agent)}>
              <Cpu size={12} />
              {modelControlText(agent)}
            </span>
          </div>
        </div>
        <Badge tone={ready ? "success" : "default"} dot>
          {agent.enabled ? (ready ? "已启用" : "待检查") : "已停用"}
        </Badge>
      </header>

      <div className="agent-facts">
        <span>
          <Wrench size={12} />
          {agent.can_write ? "可写工作区" : "只读执行"}
        </span>
        <span className="agent-command" title={commandLine(agent)}>
          <Terminal size={12} />
          <span className="mono">{commandLine(agent)}</span>
        </span>
      </div>

      <div className="agent-tags" aria-label="角色">
        {agent.roles.length ? (
          agent.roles.map((role) => (
            <span className="agent-tag" key={role}>
              {role}
            </span>
          ))
        ) : (
          <span className="agent-tag is-empty">未声明角色</span>
        )}
      </div>

      {agent.strengths.length ? (
        <div className="agent-strengths">
          强项
          <span>{agent.strengths.join(" · ")}</span>
        </div>
      ) : null}

      <div className="agent-usage-grid">
        <UsageCell label="调用" value={fmtInt(numberValue(agent.usage.invocations))} />
        <UsageCell
          label="耗时"
          value={formatSeconds(numberValue(agent.usage.seconds))}
        />
        <UsageCell label="Tokens" value={fmtInt(numberValue(agent.usage.tokens))} />
        <UsageCell label="成本" value={formatMoney(numberValue(agent.usage.cost))} />
      </div>

      <div className="agent-quota">
        <div className="agent-quota-title">
          <Gauge size={13} /> 额度用量
        </div>
        <QuotaRow
          label="调用次数"
          used={numberValue(agent.usage.invocations)}
          max={finiteNumber(agent.quota.max_invocations)}
          format={fmtInt}
        />
        <QuotaRow
          label="运行时长"
          used={numberValue(agent.usage.seconds)}
          max={finiteNumber(agent.quota.max_seconds)}
          format={formatSeconds}
        />
        <QuotaRow
          label="成本"
          used={numberValue(agent.usage.cost)}
          max={finiteNumber(agent.quota.max_cost)}
          format={formatMoney}
        />
      </div>

      {agent.error ? (
        <div className="agent-error" role="alert">
          <AlertTriangle size={13} />
          <span>{agent.error}</span>
        </div>
      ) : null}

      <footer className="agent-card-foot">
        <div className="agent-check" aria-live="polite">
          <CheckResult check={check} />
        </div>
        <Button onClick={onCheck} loading={checking} disabled={!agent.id}>
          {checking ? null : <PlugZap size={14} />} 检测连接
        </Button>
      </footer>
    </article>
  );
}

function SummaryItem({
  icon,
  label,
  value,
  hint,
  tone,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  hint: string;
  tone?: "success" | "warning";
}) {
  return (
    <div className="agents-summary-item">
      <div className="agents-summary-label">
        <span>{icon}</span>
        {label}
      </div>
      <div className={`agents-summary-value${tone ? ` is-${tone}` : ""}`}>{value}</div>
      <div className="agents-summary-hint">{hint}</div>
    </div>
  );
}

function UsageCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="agent-usage-cell">
      <div className="agent-usage-label">{label}</div>
      <div className="agent-usage-value">{value}</div>
    </div>
  );
}

function QuotaRow({
  label,
  used,
  max,
  format,
}: {
  label: string;
  used: number;
  max: number | null;
  format: (value: number) => string;
}) {
  const ratio = max && max > 0 ? used / max : 0;
  const bounded = Math.max(0, Math.min(1, ratio));
  const tone = ratio >= 1 ? " is-danger" : ratio >= 0.85 ? " is-warning" : "";

  return (
    <div className="quota-row">
      <div className="quota-meta">
        <span>{label}</span>
        <span className="mono">
          {format(used)} <span className="muted">/</span> {max === null ? "未设置" : format(max)}
        </span>
      </div>
      <div
        className="quota-track"
        role="progressbar"
        aria-label={`${label}额度使用`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(bounded * 100)}
      >
        <span className={`quota-fill${tone}`} style={{ width: `${bounded * 100}%` }} />
      </div>
    </div>
  );
}

function CheckResult({ check }: { check?: CheckEntry }) {
  if (!check) return <span className="check-muted">尚未检测</span>;
  if (check.state === "checking") return <span className="check-muted">正在握手…</span>;
  if (check.state === "ok") {
    return (
      <div className="check-result is-ok">
        <Badge tone="success">
          <CheckCircle2 size={11} /> 连接正常
        </Badge>
        <span title={`${check.detail} · ${formatCheckTime(check.checkedAt)}`}>{check.detail}</span>
      </div>
    );
  }
  return (
    <div className="check-result is-failed">
      <Badge tone="danger">
        <AlertTriangle size={11} /> 连接失败
      </Badge>
      <span title={`${check.detail} · ${formatCheckTime(check.checkedAt)}`}>{check.detail}</span>
    </div>
  );
}

function AgentsSkeleton() {
  return (
    <>
      <div className="agents-summary">
        {[0, 1, 2, 3, 4].map((index) => (
          <div className="skeleton" key={index} style={{ height: 78 }} />
        ))}
      </div>
      <div className="agent-grid" style={{ marginTop: 16 }}>
        {[0, 1, 2, 3].map((index) => (
          <div className="skeleton" key={index} style={{ height: 380 }} />
        ))}
      </div>
    </>
  );
}

function parseAgentsConfig(value: unknown): AgentsConfig {
  if (!isRecord(value)) return {};
  const defaults = isRecord(value.defaults) ? value.defaults : {};
  const defaultQuota = isRecord(defaults.quota) ? (defaults.quota as Quota) : undefined;
  const agents = Array.isArray(value.agents)
    ? value.agents.filter(isRecord).map((item) => ({
        id: typeof item.id === "string" ? item.id : undefined,
        model: typeof item.model === "string" ? item.model : null,
        quota: isRecord(item.quota) ? (item.quota as Quota) : undefined,
      }))
    : [];
  return { defaults: { quota: defaultQuota }, agents };
}

function normalizeAgent(raw: RawAgentShape, config: AgentsConfig): AgentDetail {
  const id = raw.id || raw.agent_id || "agent";
  const configured = config.agents?.find((item) => item.id === id);
  const nestedUsage = raw.usage ?? {};
  const usage = {
    invocations: numberValue(nestedUsage.invocations ?? raw.invocations),
    seconds: numberValue(nestedUsage.seconds ?? raw.seconds),
    cost: numberValue(nestedUsage.cost ?? raw.cost),
    tokens: numberValue(nestedUsage.tokens ?? raw.tokens),
  };
  const quota = { ...(config.defaults?.quota ?? {}), ...(configured?.quota ?? {}), ...(raw.quota ?? {}) };
  return {
    id,
    label: raw.label || id,
    protocol: raw.protocol || "未声明",
    enabled: raw.enabled !== false,
    command: raw.command ?? null,
    args: Array.isArray(raw.args) ? raw.args.map(String) : [],
    cwd: raw.cwd,
    roles: Array.isArray(raw.roles) ? raw.roles.map(String) : [],
    strengths: Array.isArray(raw.strengths) ? raw.strengths.map(String) : [],
    can_write: raw.can_write !== false,
    quota,
    usage,
    model: configured?.model ?? raw.model ?? null,
    error: raw.error,
  };
}

function isReady(agent: AgentDetail): boolean {
  if (!agent.enabled || agent.error) return false;
  if (agent.protocol === "human") return true;
  return Boolean(agent.command);
}

function commandLine(agent: AgentDetail): string {
  if (!agent.command) return "命令未解析";
  return [agent.command, ...agent.args].join(" ");
}

/** 参数骨架里是否真的包含 {model}（后端只替换 argv 参数，不替换命令本身）。 */
function cliInjectsModel(agent: AgentDetail): boolean {
  return agent.args.some((arg) => arg.includes("{model}"));
}

/** 按协议如实说明模型由谁控制，避免把 agents.json 的 model 误显示成“已生效”。 */
function modelControlText(agent: AgentDetail): string {
  const protocol = agent.protocol.toLowerCase();
  if (protocol === "acp") return "模型由 Agent 自身配置决定（ACP 不传递 model）";
  if (protocol === "cli" && cliInjectsModel(agent)) return "CLI 参数注入 {model}";
  return "未配置";
}

function modelControlTitle(agent: AgentDetail): string {
  const protocol = agent.protocol.toLowerCase();
  if (protocol === "acp") {
    return agent.model
      ? `ACP 不传递 model；agents.json 中的 ${agent.model} 不会生效，实际模型由 Agent 自身配置决定`
      : "ACP 不传递 model；实际模型由 Agent 自身配置决定";
  }
  if (protocol === "cli" && cliInjectsModel(agent)) {
    return agent.model
      ? `CLI 参数中的 {model} 会替换为 ${agent.model}`
      : "CLI 参数含 {model}，但 agents.json 未配置 model（会注入空字符串）";
  }
  return "未配置模型注入：agents.json 中的 model 不会传给该执行端";
}

function numberValue(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function formatMoney(value: number): string {
  return `¥${numberValue(value).toFixed(2)}`;
}

function formatSeconds(value: number): string {
  return value > 0 ? fmtDuration(value * 1000) : "0s";
}

function formatCheckTime(value: number): string {
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
