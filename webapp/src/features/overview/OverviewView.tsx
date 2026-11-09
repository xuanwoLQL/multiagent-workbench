import { useMemo } from "react";
import {
  ArrowRight,
  Bot,
  CheckCircle2,
  CircleDot,
  Clock3,
  GitBranch,
  ListChecks,
  Play,
  Plus,
  Sparkles,
} from "lucide-react";
import type { Dashboard, Task } from "../../api/types";
import { useDashboard, useGit, useInvalidateProject } from "../../app/queries";
import { useUi } from "../../app/store";
import { Button, EmptyState, Panel, SectionHeader, Stat } from "../../ui/primitives";
import { TaskRow } from "../../components/TaskRow";
import { agentColor, agentKey, fmtDuration, fmtRelative, initials, shortPath } from "../../lib/format";

export function OverviewView({
  onOpenTask,
  onNewTask,
  onViewTasks,
}: {
  onOpenTask: (taskId: string, tab?: "overview" | "changes" | "activity" | "logs" | "context") => void;
  onNewTask: () => void;
  onViewTasks: () => void;
}) {
  const projectId = useUi((state) => state.projectId);
  const dashboard = useDashboard(projectId);
  const git = useGit(projectId);
  const invalidate = useInvalidateProject(projectId);
  const data = dashboard.data;

  const groups = useMemo(() => groupTasks(data), [data]);

  const start = async () => {
    if (!projectId) return;
    await fetch(`/api/projects/${projectId}/run`, { method: "POST" });
    invalidate();
  };
  const stop = async () => {
    if (!projectId) return;
    await fetch(`/api/projects/${projectId}/stop`, { method: "POST" });
    invalidate();
  };

  if (!data) {
    return <OverviewSkeleton />;
  }

  const total = data.tasks.length;
  const successRate =
    total > 0 ? Math.round(((data.progress.counters.success ?? 0) / total) * 100) : 0;

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head-text">
          <div className="page-title">
            <h1>{data.project.label || data.project.name}</h1>
            {data.running ? (
              <span className="live-pill">
                <span className="live-dot" /> 运行中
              </span>
            ) : null}
          </div>
          <div className="page-subtitle project-line">
            <span className="mono">{shortPath(data.project.root_path, 4)}</span>
            {git.data?.is_repo ? (
              <>
                <span className="dot-sep" />
                <GitBranch size={12} />
                <span>{git.data.branch}</span>
                {git.data.dirty ? <span className="dirty-dot" title="有未提交改动" /> : null}
              </>
            ) : (
              <>
                <span className="dot-sep" />
                <span>非 Git 目录</span>
              </>
            )}
          </div>
        </div>
        <div className="page-actions">
          {data.running ? (
            <Button variant="danger" onClick={() => void stop()}>
              停止运行
            </Button>
          ) : (
            <Button onClick={() => void start()} disabled={!groups.pending.length}>
              <Play size={14} /> 开始运行
            </Button>
          )}
          <Button variant="primary" onClick={onNewTask}>
            <Plus size={14} /> 新任务
          </Button>
        </div>
      </div>

      {total === 0 ? (
        <FirstRun onNewTask={onNewTask} />
      ) : (
        <>
          <div className="overview-stats">
            <Stat label="任务总数" value={total} hint="当前工作区" />
            <Stat
              label="运行中"
              value={groups.running.length}
              tone={groups.running.length ? "default" : undefined}
              hint={groups.running.length ? "Agent 正在工作" : "无活动"}
            />
            <Stat
              label="需要你处理"
              value={groups.attention.length}
              tone={groups.attention.length ? "warning" : undefined}
              hint="失败、阻塞或待确认"
            />
            <Stat
              label="完成率"
              value={`${successRate}%`}
              tone={successRate >= 70 ? "success" : undefined}
              hint={`${data.progress.counters.success ?? 0} / ${total} 成功`}
            />
          </div>

          <div className="grid-side" style={{ marginTop: 16 }}>
            <div className="stack">
              <Panel>
                <SectionHeader
                  title={
                    <span className="row">
                      <CircleDot size={15} className={groups.running.length ? "text-accent" : "muted"} />
                      当前运行
                    </span>
                  }
                  subtitle={groups.running.length ? "实时状态" : "没有 Agent 在跑"}
                  actions={
                    groups.running.length ? (
                      <Button size="sm" variant="ghost" onClick={onViewTasks}>
                        全部任务 <ArrowRight size={13} />
                      </Button>
                    ) : null
                  }
                />
                <div className="list-block">
                  {groups.running.length ? (
                    groups.running.map((task) => (
                      <div key={task.id} className="running-row" onClick={() => onOpenTask(task.id, "activity")}>
                        <span className="pulse-ring" />
                        <div className="running-main">
                          <div className="running-title truncate">{task.title}</div>
                          <div className="running-meta">
                            {task.agent_id || "自动"} · 第{" "}
                            {Number(task.runtime?.attempt ?? task.attempt ?? 0) + 1} 次尝试
                          </div>
                        </div>
                        <span className="running-time mono">
                          {task.runtime?.elapsed_sec != null
                            ? fmtDuration(Number(task.runtime.elapsed_sec) * 1000)
                            : task.started_at
                              ? elapsed(task.started_at)
                              : "—"}
                        </span>
                        <ArrowRight size={14} className="muted" />
                      </div>
                    ))
                  ) : (
                    <div className="inline-empty">
                      队列空闲。把任务加入队列后点击「开始运行」，Agent 会自动接手。
                    </div>
                  )}
                </div>
              </Panel>

              {groups.attention.length ? (
                <Panel>
                  <SectionHeader
                    title={
                      <span className="row">
                        <Clock3 size={15} className="text-warn" /> 需要你处理
                      </span>
                    }
                    subtitle="卡住的任务不会静默等待"
                    actions={
                      <Button size="sm" variant="ghost" onClick={onViewTasks}>
                        查看全部 <ArrowRight size={13} />
                      </Button>
                    }
                  />
                  <div className="list-block">
                    {groups.attention.slice(0, 6).map((task) => (
                      <TaskRow key={task.id} task={task} onOpen={({ id }) => onOpenTask(id)} />
                    ))}
                  </div>
                </Panel>
              ) : null}

              <Panel>
                <SectionHeader
                  title={
                    <span className="row">
                      <ListChecks size={15} /> 队列
                    </span>
                  }
                  subtitle={`${groups.pending.length} 个任务等待执行`}
                  actions={
                    <Button size="sm" variant="ghost" onClick={onViewTasks}>
                      全部任务 <ArrowRight size={13} />
                    </Button>
                  }
                />
                <div className="list-block">
                  {groups.pending.length ? (
                    groups.pending
                      .slice(0, 8)
                      .map((task) => (
                        <TaskRow key={task.id} task={task} onOpen={({ id }) => onOpenTask(id)} />
                      ))
                  ) : (
                    <div className="inline-empty">队列为空。</div>
                  )}
                </div>
              </Panel>
            </div>

            <div className="stack">
              <Panel>
                <SectionHeader title="Agent" subtitle={`${data.agents.length} 个执行端`} />
                <div className="agent-list">
                  {data.agents.map((agent) => { const key = agentKey(agent); return (
                    <div className="agent-row" key={key}>
                      <span
                        className="agent-avatar"
                        style={{
                          background: `${agentColor(key)}1a`,
                          color: agentColor(key),
                        }}
                      >
                        {initials(agent.label || key)}
                      </span>
                      <div className="agent-info">
                        <div className="agent-name">{agent.label || key}</div>
                        <div className="agent-sub">
                          {agent.protocol || "cli"} · {agent.roles?.slice(0, 2).join(" / ") || "worker"}
                        </div>
                      </div>
                      <span className="agent-usage">
                        {agent.usage?.invocations ?? agent.invocations ?? 0}
                      </span>
                      <span className={agent.enabled === false ? "dot is-off" : "dot"} />
                    </div>
                  ); })}
                </div>
              </Panel>

              <Panel>
                <SectionHeader
                  title="最近活动"
                  subtitle="工作区事件"
                  actions={
                    <Button size="sm" variant="ghost" onClick={onViewTasks}>
                      任务 <ArrowRight size={13} />
                    </Button>
                  }
                />
                <div className="activity-list">
                  {data.activity.length ? (
                    data.activity.slice(0, 8).map((event, index) => (
                      <div className="activity-row" key={`${event.id ?? index}-${event.at ?? index}`}>
                        <span className="activity-dot" />
                        <div className="activity-main">
                          <div className="activity-text">{event.summary || event.message || event.kind}</div>
                          <div className="activity-meta">
                            {event.kind} · {fmtRelative(event.at || event.created_at)}
                          </div>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="inline-empty">还没有活动记录。</div>
                  )}
                </div>
              </Panel>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function groupTasks(data?: Dashboard) {
  const tasks = data?.tasks ?? [];
  return {
    running: tasks.filter((task) => task.status === "running"),
    attention: tasks.filter((task) => ["failed", "blocked", "waiting"].includes(task.status)),
    pending: tasks.filter((task) => ["pending", "interrupted"].includes(task.status)),
  };
}

function elapsed(startedAt: string): string {
  return fmtDuration(Date.now() - new Date(startedAt).getTime());
}

function FirstRun({ onNewTask }: { onNewTask: () => void }) {
  const templates = [
    "搭一个可运行的 MVP",
    "把现有项目补齐测试",
    "重构一个模块",
    "写一份技术方案",
  ];
  return (
    <Panel className="first-run">
      <div className="first-run-inner">
        <div className="first-run-icon">
          <Sparkles size={22} />
        </div>
        <h2>从一个目标开始</h2>
        <p>
          描述你想完成的事，工作台会把它拆成任务、分配 Agent，并在隔离副本里执行。
          你只需要审阅结果。
        </p>
        <div className="template-row">
          {templates.map((template) => (
            <button key={template} type="button" className="template-chip" onClick={onNewTask}>
              {template}
            </button>
          ))}
        </div>
        <Button variant="primary" size="lg" onClick={onNewTask}>
          <Plus size={15} /> 创建第一个任务
        </Button>
        <div className="first-run-note">
          <CheckCircle2 size={13} /> 所有改动先落在隔离副本，验收通过后才同步。
        </div>
      </div>
    </Panel>
  );
}

function OverviewSkeleton() {
  return (
    <div className="page">
      <div className="skeleton" style={{ width: 240, height: 28 }} />
      <div className="skeleton" style={{ width: 360, height: 14, marginTop: 8 }} />
      <div className="overview-stats" style={{ marginTop: 22 }}>
        {[0, 1, 2, 3].map((index) => (
          <div key={index} className="skeleton" style={{ height: 52 }} />
        ))}
      </div>
      <div className="skeleton" style={{ height: 260, marginTop: 16 }} />
    </div>
  );
}
