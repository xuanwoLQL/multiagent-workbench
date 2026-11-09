import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  GitBranch,
  Play,
  RefreshCw,
} from "lucide-react";
import { api, qk } from "../../api/client";
import { useUi } from "../../app/store";
import { Badge, Button } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import {
  agentReady,
  useAgentsDetail,
  useProjectDashboard,
  useProjectGit,
  type AgentRecord,
  type ProjectRecord,
} from "./project-api";

export function StartWorkPanel({
  project,
  onEnter,
  onBack,
}: {
  project: ProjectRecord;
  onEnter: (id: string) => void;
  onBack: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const agentsQuery = useAgentsDetail();
  const dashboardQuery = useProjectDashboard(project.id);
  const gitQuery = useProjectGit(project.id);

  const [runBusy, setRunBusy] = useState(false);
  const [ran, setRan] = useState(false);

  const agents = agentsQuery.data ?? [];
  const ready = agents.filter(agentReady);
  const tasks = dashboardQuery.data?.tasks ?? [];
  const pending = tasks.filter((task) => task.status === "pending" || task.status === "interrupted");
  const running = Boolean(dashboardQuery.data?.running) || tasks.some((task) => task.status === "running");

  const openTaskDialog = () => {
    useUi.getState().setProject(project.id);
    useUi.getState().setNewTaskOpen(true);
    onEnter(project.id);
  };

  const runQueue = async () => {
    setRunBusy(true);
    try {
      const result = await api.post<{ accepted?: boolean; message?: string }>(
        `/api/projects/${project.id}/run`,
        {},
      );
      if (result.accepted === false) {
        toast.info(result.message || "调度已在运行");
      } else {
        setRan(true);
        toast.success("队列已开始执行", "可以在任务页跟踪每一次 Agent 调用。");
      }
      await client.invalidateQueries({ queryKey: qk.dashboard(project.id) });
    } catch (error) {
      toast.error("启动队列失败", errorMessage(error));
    } finally {
      setRunBusy(false);
    }
  };

  return (
    <section className="ob-open" aria-label="开始工作">
      <header className="ob-open-head">
        <div className="ob-open-id">
          <span className="ob-open-avatar">
            {initials(project.label || project.name)}
          </span>
          <div className="ob-open-text">
            <h2>{project.label || project.name}</h2>
            <div className="ob-open-path mono" title={project.root_path}>
              {project.root_path}
            </div>
          </div>
        </div>
        <div className="ob-open-actions">
          <Button size="sm" variant="ghost" onClick={onBack}>
            <ArrowLeft size={13} /> 换一个项目
          </Button>
          <Button variant="primary" onClick={() => onEnter(project.id)}>
            进入工作台 <ArrowRight size={14} />
          </Button>
        </div>
      </header>

      <div className="ob-facts">
        <span className="ob-fact" title="导入或创建时已成功写入 .multiagent/project.json">
          <Check size={12} /> 目录可写
        </span>
        <span className="ob-fact-sep" />
        <GitFact query={gitQuery} />
        <span className="ob-fact-sep" />
        <span className="ob-fact">
          {dashboardQuery.data
            ? `${tasks.length} 个任务 · ${pending.length} 个待执行`
            : dashboardQuery.isError
              ? "队列状态不可用"
              : "读取队列…"}
        </span>
        <span className="ob-fact-sep" />
        <span className="ob-fact">
          {agentsQuery.data
            ? `${ready.length} 个 Agent 就绪`
            : agentsQuery.isError
              ? "Agent 状态不可用"
              : "检查 Agent…"}
        </span>
      </div>

      <div className="ob-work-head">
        <h3>开始工作</h3>
        <span>完成这三步，Agent 就会接手。</span>
      </div>

      <ol className="ob-steps">
        <li className={`ob-step${ready.length ? " is-done" : ""}`}>
          <span className="ob-step-no">{ready.length ? <Check size={12} /> : "1"}</span>
          <div className="ob-step-main">
            <div className="ob-step-head">
              <div>
                <div className="ob-step-title">检查 Agent 就绪</div>
                <p className="ob-step-desc">
                  读取本机 Agent 的命令与启用状态。至少要有一个可用 Agent，队列才能开始执行。
                </p>
              </div>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void agentsQuery.refetch()}
                loading={agentsQuery.isFetching && !agentsQuery.isLoading}
              >
                <RefreshCw size={13} /> 重新检查
              </Button>
            </div>
            <AgentStatus query={agentsQuery} ready={ready} />
          </div>
        </li>

        <li className={`ob-step${tasks.length ? " is-done" : ""}`}>
          <span className="ob-step-no">{tasks.length ? <Check size={12} /> : "2"}</span>
          <div className="ob-step-main">
            <div className="ob-step-head">
              <div>
                <div className="ob-step-title">
                  {tasks.length ? "继续添加任务" : "创建第一个任务"}
                </div>
                <p className="ob-step-desc">
                  用一句话描述目标；工作台会拆出任务与依赖，在隔离副本里执行，验收通过后再同步。
                </p>
              </div>
              <Button size="sm" onClick={openTaskDialog}>
                <ArrowRight size={13} /> 新建任务
              </Button>
            </div>
            <div className="ob-step-status">
              {dashboardQuery.isLoading ? (
                <span>读取队列…</span>
              ) : tasks.length ? (
                <Badge tone="success" dot>
                  {tasks.length} 个任务已登记
                </Badge>
              ) : dashboardQuery.isError ? (
                <span className="is-danger">无法读取任务列表</span>
              ) : (
                <span>还没有任务。创建后会显示在队列里。</span>
              )}
            </div>
          </div>
        </li>

        <li className={`ob-step${running || ran ? " is-done" : ""}`}>
          <span className="ob-step-no">{running || ran ? <Check size={12} /> : "3"}</span>
          <div className="ob-step-main">
            <div className="ob-step-head">
              <div>
                <div className="ob-step-title">运行队列</div>
                <p className="ob-step-desc">
                  调度器会按依赖与并发上限派发任务；失败会按重试预算自动再试，阻塞时停下来等你处理。
                </p>
              </div>
              <Button
                size="sm"
                variant="primary"
                onClick={() => void runQueue()}
                loading={runBusy}
                disabled={
                  runBusy ||
                  running ||
                  dashboardQuery.isLoading ||
                  Boolean(dashboardQuery.isError) ||
                  pending.length === 0
                }
              >
                <Play size={13} /> {running ? "正在运行" : "开始运行"}
              </Button>
            </div>
            <div className="ob-step-status">
              {running ? (
                <Badge tone="accent" dot>
                  调度运行中
                </Badge>
              ) : ran ? (
                <span className="is-success">已启动，可在任务页跟踪进度。</span>
              ) : dashboardQuery.isError ? (
                <span className="is-danger">无法读取运行状态</span>
              ) : pending.length ? (
                <span>{pending.length} 个待执行任务，随时可以开始。</span>
              ) : (
                <span>队列为空。先创建任务，再回来开始运行。</span>
              )}
            </div>
          </div>
        </li>
      </ol>
    </section>
  );
}

function AgentStatus({
  query,
  ready,
}: {
  query: ReturnType<typeof useAgentsDetail>;
  ready: AgentRecord[];
}) {
  if (query.isLoading) {
    return (
      <div className="ob-step-status">
        <span className="skeleton" style={{ width: 160, height: 12 }} />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="ob-step-status is-danger">
        <AlertTriangle size={13} />
        <span>无法读取 Agent 状态</span>
        <button type="button" className="ob-inline-link" onClick={() => void query.refetch()}>
          重试
        </button>
      </div>
    );
  }
  const all = query.data ?? [];
  if (!all.length) {
    return (
      <div className="ob-step-status is-warning">
        <AlertTriangle size={13} />
        <span>没有配置任何 Agent。请在「设置 → Agent 配置」中添加执行端。</span>
      </div>
    );
  }
  const readyCount = ready?.length ?? 0;
  if (!readyCount) {
    return (
      <div className="ob-step-status is-warning">
        <AlertTriangle size={13} />
        <span>
          已配置 {all.length} 个 Agent，但命令均未就绪。请在设置中检查命令路径。
        </span>
      </div>
    );
  }
  return (
    <div className="ob-step-status">
      <span className="is-success">{readyCount} 个就绪</span>
      {all.slice(0, 5).map((agent) => {
        const ok = agentReady(agent);
        return (
          <span
            key={String(agent.id || agent.agent_id || agent.label || "agent")}
            className={`ob-agent-chip${ok ? "" : " is-off"}`}
            title={agent.error || (ok ? undefined : "未就绪")}
          >
            <span className="dot" />
            {agent.label || agent.id || agent.agent_id || "Agent"}
          </span>
        );
      })}
      {all.length > 5 ? <span>+{all.length - 5}</span> : null}
    </div>
  );
}

function GitFact({
  query,
}: {
  query: ReturnType<typeof useProjectGit>;
}) {
  if (query.isLoading) return <span className="ob-fact">检测 Git…</span>;
  if (query.isError) return <span className="ob-fact">Git 状态不可用</span>;
  const git = query.data;
  if (!git?.is_repo) {
    return <span className="ob-fact">非 Git 目录 · 任务使用隔离副本</span>;
  }
  return (
    <span className="ob-fact">
      <GitBranch size={12} />
      {git.branch || "detached"}
      {git.dirty ? <Badge tone="warning">有未提交改动</Badge> : null}
    </span>
  );
}

function initials(value: string): string {
  const clean = (value || "?").trim();
  const parts = clean.split(/[\s·/_-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return clean.slice(0, 2).toUpperCase();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

