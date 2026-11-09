import {
  Braces,
  Copy,
  FileText,
  FolderGit2,
  GitBranch,
  Layers,
  ListChecks,
  LockKeyhole,
  Package,
  ShieldCheck,
} from "lucide-react";
import type { ReactNode } from "react";
import type { Task, TaskDetail } from "../../api/types";
import { StatusBadge } from "../../components/StatusBadge";
import { Badge, Button, Panel, SectionHeader } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import {
  agentColor,
  fmtDateTime,
  fmtDuration,
  initials,
} from "../../lib/format";
import { asArray, copyText, safeJson } from "./helpers";

function IsolationBadge({ isolation }: { isolation: Task["isolation"] }) {
  if (isolation === "worktree") {
    return (
      <Badge tone="info">
        <GitBranch size={11} /> Git worktree
      </Badge>
    );
  }
  if (isolation === "shared") {
    return (
      <Badge tone="warning">
        <FolderGit2 size={11} /> 共享工作区
      </Badge>
    );
  }
  return (
    <Badge tone="accent">
      <Layers size={11} /> 隔离副本
    </Badge>
  );
}

function isolationHint(isolation: Task["isolation"], readOnly: boolean): string {
  if (readOnly) return "只读任务在独立副本中执行，不会改动项目工作区。";
  if (isolation === "worktree") return "Agent 在独立 worktree 中工作，改动可单独审查与合并。";
  if (isolation === "shared") return "Agent 直接写入项目目录；没有验收后的同步步骤。";
  return "Agent 在独立工作副本中执行，验收通过后把 write_scope 内的改动同步回工作区。";
}

function Chips({ values, empty }: { values: string[]; empty: string }) {
  if (!values.length) return <span className="td-context-empty">{empty}</span>;
  return (
    <div className="td-chip-list">
      {values.map((value, index) => (
        <code key={`${value}-${index}`} className="td-chip">
          {value}
        </code>
      ))}
    </div>
  );
}

function JsonCard({
  title,
  subtitle,
  icon,
  value,
  emptyHint,
}: {
  title: string;
  subtitle: string;
  icon: ReactNode;
  value: unknown;
  emptyHint?: string;
}) {
  const toast = useToast();
  const text = safeJson(value);
  const empty = value === null || value === undefined || (Array.isArray(value) && !value.length);
  const copy = async () => {
    const ok = await copyText(text);
    if (ok) toast.success(`已复制${title}`);
    else toast.error("复制失败");
  };
  return (
    <Panel className="td-panel td-json-card">
      <SectionHeader
        title={
          <span className="td-section-title">
            {icon} {title}
          </span>
        }
        subtitle={subtitle}
        actions={
          <Button size="sm" variant="ghost" onClick={() => void copy()}>
            <Copy size={13} /> 复制
          </Button>
        }
      />
      {empty && emptyHint ? <div className="td-json-empty-hint">{emptyHint}</div> : null}
      <pre className="td-json">{text}</pre>
    </Panel>
  );
}

export function ContextTab({ detail }: { detail: TaskDetail }) {
  const task = detail.task;
  const dependencies = asArray(detail.dependencies) as Task[];
  const handoff = detail.handoff ?? null;
  const acceptance = Array.isArray(task.acceptance) ? task.acceptance : [];
  const inputs = asArray(task.inputs);
  const outputs = asArray(task.outputs);

  return (
    <div className="td-context">
      <div className="td-context-grid">
        <div className="td-context-main">
          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <FileText size={15} /> 任务说明
                </span>
              }
              subtitle="发送给 Agent 的完整任务上下文"
            />
            {task.brief?.trim() ? (
              <div className="td-brief">{task.brief}</div>
            ) : (
              <div className="td-inline-empty">没有单独的任务说明，只有标题与产出要求。</div>
            )}
          </Panel>

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <ShieldCheck size={15} /> 执行约束
                </span>
              }
              subtitle="写入范围、隔离方式与预算"
            />
            <div className="td-context-grid-2">
              <div className="td-context-field">
                <div className="td-context-label">写入方式</div>
                <div className="td-context-value">
                  <IsolationBadge isolation={task.isolation} />
                  {task.read_only ? (
                    <Badge tone="outline">
                      <LockKeyhole size={11} /> 只读
                    </Badge>
                  ) : null}
                </div>
                <div className="td-context-hint">{isolationHint(task.isolation, task.read_only)}</div>
              </div>

              <div className="td-context-field">
                <div className="td-context-label">写范围</div>
                <Chips values={asArray(task.write_scope)} empty="整个工作区" />
                <div className="td-context-hint">
                  同步时只回收集合内、非保护路径、未冲突的文件。
                </div>
              </div>

              <div className="td-context-field">
                <div className="td-context-label">执行预算</div>
                <div className="td-context-pairs">
                  <span>超时</span>
                  <strong>{task.timeout_sec ? fmtDuration(task.timeout_sec * 1000) : "默认"}</strong>
                  <span>尝试</span>
                  <strong>
                    {task.attempt || 0} / {task.max_attempts || "—"}
                  </strong>
                  <span>优先级</span>
                  <strong>P{task.priority ?? 0}</strong>
                  <span>角色</span>
                  <strong>{task.role || "worker"}</strong>
                </div>
              </div>

              <div className="td-context-field">
                <div className="td-context-label">执行者</div>
                <div className="td-context-value">
                  {task.agent_id ? (
                    <span className="td-context-agent">
                      <span
                        className="td-avatar"
                        style={{
                          background: `${agentColor(task.agent_id)}1a`,
                          color: agentColor(task.agent_id),
                        }}
                      >
                        {initials(task.agent_id)}
                      </span>
                      {task.agent_id}
                    </span>
                  ) : (
                    <span className="td-context-empty">自动路由</span>
                  )}
                  <Badge tone="outline">{task.kind}</Badge>
                </div>
                <div className="td-context-hint">
                  创建于 {fmtDateTime(task.created_at)} · 更新于 {fmtDateTime(task.updated_at)}
                </div>
              </div>
            </div>
          </Panel>

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <Package size={15} /> 输入与产出
                </span>
              }
              subtitle="任务声明需要读取与写出的路径"
            />
            <div className="td-context-grid-2">
              <div className="td-context-field">
                <div className="td-context-label">输入</div>
                <Chips values={inputs} empty="没有声明输入文件" />
              </div>
              <div className="td-context-field">
                <div className="td-context-label">产出</div>
                <Chips values={outputs} empty="没有声明固定产出文件" />
              </div>
            </div>
          </Panel>

          <Panel className="td-panel">
            <SectionHeader
              title={
                <span className="td-section-title">
                  <ListChecks size={15} /> 依赖任务
                </span>
              }
              subtitle={dependencies.length ? `${dependencies.length} 个前置任务` : "没有前置依赖"}
            />
            {dependencies.length ? (
              <ul className="td-deps">
                {dependencies.map((dependency) => (
                  <li key={dependency.id} className="td-dep">
                    <span
                      className="td-avatar"
                      style={{
                        background: `${agentColor(dependency.agent_id || "auto")}1a`,
                        color: agentColor(dependency.agent_id || "auto"),
                      }}
                    >
                      {initials(dependency.agent_id || "自动")}
                    </span>
                    <div className="td-dep-main">
                      <div className="td-dep-title">{dependency.title || dependency.id}</div>
                      <div className="td-dep-meta">
                        <span>{dependency.kind}</span>
                        <span>{dependency.agent_id || "自动路由"}</span>
                        <span>{fmtDateTime(dependency.updated_at)}</span>
                      </div>
                    </div>
                    <StatusBadge status={dependency.status} />
                  </li>
                ))}
              </ul>
            ) : (
              <div className="td-inline-empty">
                没有前置依赖，任务可以在调度器空闲时立即被派发。
              </div>
            )}
          </Panel>
        </div>

        <div className="td-context-side">
          <JsonCard
            title="交接 JSON"
            subtitle="Agent 留下的结构化交接"
            icon={<Layers size={15} />}
            value={handoff ?? {}}
            emptyHint="还没有交接记录，任务完成后会自动生成。"
          />
          <JsonCard
            title="验收 JSON"
            subtitle="任务成功必须满足的条件"
            icon={<Braces size={15} />}
            value={acceptance}
            emptyHint="使用任务类型的默认验收规则。"
          />
          <JsonCard
            title="运行结果 JSON"
            subtitle="runner 写回的最终结果"
            icon={<Braces size={15} />}
            value={task.result ?? {}}
            emptyHint="任务尚未产生运行结果。"
          />
        </div>
      </div>
    </div>
  );
}
