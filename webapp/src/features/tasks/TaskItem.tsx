import { Clock3 } from "lucide-react";
import type { Task } from "../../api/types";
import { StatusBadge } from "../../components/StatusBadge";
import {
  KIND_LABEL,
  agentColor,
  fmtDateTime,
  fmtRelative,
  initials,
} from "../../lib/format";
import { attemptLabel, elapsedOf, priorityTone } from "./taskUtils";

interface ItemProps {
  task: Task;
  agentLabel: string;
  now: number;
  selected: boolean;
  onOpen: (taskId: string) => void;
  onToggle: (taskId: string) => void;
}

function avatarStyle(agentId: string) {
  const color = agentColor(agentId);
  return { background: `${color}1a`, color };
}

function taskTitle(task: Task): string {
  return task.title?.trim() || "未命名任务";
}

export function TaskCard({ task, agentLabel, now, selected, onOpen, onToggle }: ItemProps) {
  const title = taskTitle(task);
  const agentId = task.agent_id || "auto";
  const priority = task.priority ?? 0;
  const kind = KIND_LABEL[task.kind] ?? task.kind ?? "通用";

  return (
    <div className={`tk-card-wrap${selected ? " is-selected" : ""}`}>
      <input
        type="checkbox"
        className="tk-check"
        checked={selected}
        onChange={() => onToggle(task.id)}
        aria-label={`选择任务：${title}`}
      />
      <button
        type="button"
        className="tk-card"
        onClick={() => onOpen(task.id)}
        title={title}
      >
        <span className="tk-card-top">
          <span className="tk-agent-avatar" style={avatarStyle(agentId)} aria-hidden="true">
            {initials(agentLabel)}
          </span>
          <span className="tk-agent-name truncate">{agentLabel}</span>
          <StatusBadge status={task.status} />
        </span>
        <span className="tk-card-title">{title}</span>
        <span className="tk-card-meta">
          <span className="tk-meta-kind">{kind}</span>
          <span className="tk-dot" />
          <span>尝试 {attemptLabel(task)}</span>
        </span>
        <span className="tk-card-foot">
          <span className={`tk-prio is-${priorityTone(priority)}`}>P{priority}</span>
          <span className="tk-foot-item">
            <Clock3 size={11} aria-hidden="true" />
            {elapsedOf(task, now)}
          </span>
          <span className="tk-foot-updated" title={fmtDateTime(task.updated_at)}>
            {fmtRelative(task.updated_at)}
          </span>
        </span>
      </button>
    </div>
  );
}

export function TaskListRow({ task, agentLabel, now, selected, onOpen, onToggle }: ItemProps) {
  const title = taskTitle(task);
  const agentId = task.agent_id || "auto";
  const priority = task.priority ?? 0;
  const kind = KIND_LABEL[task.kind] ?? task.kind ?? "通用";

  return (
    <div className={`tk-list-row${selected ? " is-selected" : ""}`}>
      <input
        type="checkbox"
        className="tk-check tk-check-row"
        checked={selected}
        onChange={() => onToggle(task.id)}
        aria-label={`选择任务：${title}`}
      />
      <button
        type="button"
        className="tk-row-open"
        onClick={() => onOpen(task.id)}
        title={title}
      >
        <span className="tk-list-cols">
          <span className="tk-c-title">
            <span className="tk-row-title truncate">{title}</span>
            {(task.tags ?? []).length ? (
              <span className="tk-row-tag truncate">{task.tags[0]}</span>
            ) : null}
          </span>
          <span className="tk-c-agent">
            <span className="tk-agent-avatar is-tiny" style={avatarStyle(agentId)} aria-hidden="true">
              {initials(agentLabel)}
            </span>
            <span className="truncate">{agentLabel}</span>
          </span>
          <span className="tk-c-kind truncate">{kind}</span>
          <span className="tk-c-status">
            <StatusBadge status={task.status} />
          </span>
          <span className="tk-c-attempt mono">{attemptLabel(task)}</span>
          <span className="tk-c-elapsed mono">{elapsedOf(task, now)}</span>
          <span className="tk-c-prio">
            <span className={`tk-prio is-${priorityTone(priority)}`}>P{priority}</span>
          </span>
          <span className="tk-c-updated" title={fmtDateTime(task.updated_at)}>
            {fmtRelative(task.updated_at)}
          </span>
        </span>
      </button>
    </div>
  );
}

export function TaskListHeader() {
  return (
    <div className="tk-list-head" aria-hidden="true">
      <span className="tk-head-check" />
      <span className="tk-list-cols">
        <span className="tk-c-title">任务</span>
        <span className="tk-c-agent">Agent</span>
        <span className="tk-c-kind">类型</span>
        <span className="tk-c-status">状态</span>
        <span className="tk-c-attempt">尝试</span>
        <span className="tk-c-elapsed">耗时</span>
        <span className="tk-c-prio">优先级</span>
        <span className="tk-c-updated">更新时间</span>
      </span>
    </div>
  );
}
