import type { Dashboard } from "../api/types";

export function StatusBar({
  connected,
  dashboard,
  branch,
}: {
  connected: boolean;
  dashboard?: Dashboard;
  branch?: string;
}) {
  const tasks = dashboard?.tasks ?? [];
  const running = tasks.filter((task) => task.status === "running").length;
  const pending = tasks.filter((task) => task.status === "pending").length;
  const ready = (dashboard?.agents ?? []).filter((agent) => agent.enabled !== false).length;

  return (
    <footer className="statusbar">
      <span className="item">
        <span className={connected ? "dot" : "dot is-bad"} />
        {connected ? "实时已连接" : "离线"}
      </span>
      <span className="item">
        <span className={dashboard?.running ? "dot" : "dot is-idle"} />
        {dashboard?.running ? "调度运行中" : "调度空闲"}
      </span>
      <span className="item">
        Agent {ready}/{dashboard?.agents?.length ?? ready}
      </span>
      <span className="item">{running} 运行 · {pending} 排队</span>
      {branch ? <span className="item mono">{branch}</span> : null}
      <span className="statusbar-spacer" />
      <span className="item mono">{dashboard?.project?.root_path ?? "本地工作区"}</span>
    </footer>
  );
}
