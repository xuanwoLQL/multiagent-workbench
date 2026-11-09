import clsx from "clsx";
import { ChevronRight, GitBranch, ShieldCheck } from "lucide-react";
import type { Task } from "../api/types";
import { KIND_LABEL, agentColor, fmtRelative, initials } from "../lib/format";
import { StatusBadge } from "./StatusBadge";

export function TaskRow({
  task,
  active,
  onOpen,
  showProject,
}: {
  task: Task;
  active?: boolean;
  onOpen?: (task: Task) => void;
  showProject?: boolean;
}) {
  const agent = task.agent_id || "auto";
  return (
    <button
      type="button"
      className={clsx("task-row", active && "is-active")}
      onClick={() => onOpen?.(task)}
    >
      <span className="task-agent" style={{ background: `${agentColor(agent)}1a`, color: agentColor(agent) }}>
        {initials(agent === "auto" ? "自动" : agent)}
      </span>
      <span className="task-main">
        <span className="task-title truncate">{task.title || "未命名任务"}</span>
        <span className="task-meta">
          <span>{agent === "auto" ? "自动选择" : agent}</span>
          <span className="task-meta-dot" />
          <span>{KIND_LABEL[task.kind] ?? task.kind}</span>
          {task.isolation === "worktree" ? (
            <>
              <span className="task-meta-dot" />
              <GitBranch size={11} />
              <span>worktree</span>
            </>
          ) : null}
          {task.read_only ? (
            <>
              <span className="task-meta-dot" />
              <ShieldCheck size={11} />
              <span>只读</span>
            </>
          ) : null}
          <span className="task-meta-dot" />
          <span>{fmtRelative(task.updated_at)}</span>
        </span>
      </span>
      <span className="task-side">
        {task.priority >= 2 ? <span className="prio">P{task.priority}</span> : null}
        <StatusBadge status={task.status} />
      </span>
      <ChevronRight size={14} className="task-chevron" />
    </button>
  );
}
