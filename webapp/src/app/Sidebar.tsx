import {
  Activity,
  Bot,
  BookOpen,
  FolderTree,
  FolderKanban,
  GitPullRequest,
  LayoutDashboard,
  ListChecks,
  Settings,
} from "lucide-react";
import clsx from "clsx";
import type { AgentUsage, Dashboard } from "../api/types";
import { useUi, type ViewKey } from "./store";
import { agentColor, agentKey, initials } from "../lib/format";

const NAV: Array<{
  group: string;
  items: Array<{ key: ViewKey; label: string; icon: typeof LayoutDashboard }>;
}> = [
  {
    group: "工作区",
    items: [
      { key: "projects", label: "项目", icon: FolderKanban },
      { key: "overview", label: "概览", icon: LayoutDashboard },
      { key: "tasks", label: "任务", icon: ListChecks },
      { key: "runs", label: "运行", icon: Activity },
      { key: "files", label: "文件", icon: FolderTree },
      { key: "review", label: "审阅", icon: GitPullRequest },
      { key: "memory", label: "记忆", icon: BookOpen },
    ],
  },
  {
    group: "系统",
    items: [
      { key: "agents", label: "Agent", icon: Bot },
      { key: "settings", label: "设置", icon: Settings },
    ],
  },
];

export function Sidebar({
  dashboard,
  agents,
}: {
  dashboard?: Dashboard;
  agents?: AgentUsage[];
}) {
  const view = useUi((s) => s.view);
  const setView = useUi((s) => s.setView);
  const tasks = dashboard?.tasks ?? [];
  const counts: Partial<Record<ViewKey, number>> = {
    tasks: tasks.filter((task) => ["pending", "running", "waiting", "blocked"].includes(task.status)).length,
    review: tasks.filter((task) => task.status === "success").length,
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-scroll">
        {NAV.map((group) => (
          <div className="nav-group" key={group.group}>
            <div className="nav-label">{group.group}</div>
            {group.items.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  type="button"
                  key={item.key}
                  className={clsx("nav-item", view === item.key && "is-active")}
                  onClick={() => setView(item.key)}
                >
                  <Icon size={15} className="nav-icon" />
                  <span>{item.label}</span>
                  {counts[item.key] ? <span className="nav-count">{counts[item.key]}</span> : null}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <div className="sidebar-foot">
        {(agents ?? []).map((agent) => { const key = agentKey(agent); return (
          <div className="agent-mini" key={key} title={`${agent.label || key} · ${agent.protocol || ""}`}>
            <span
              className="agent-avatar"
              style={{ background: `${agentColor(key)}1a`, color: agentColor(key) }}
            >
              {initials(agent.label || key)}
            </span>
            <span className="truncate">{agent.label || key}</span>
            <span className="count">{agent.usage?.invocations ?? agent.invocations ?? 0}</span>
          </div>
        ); })}
      </div>
    </aside>
  );
}
