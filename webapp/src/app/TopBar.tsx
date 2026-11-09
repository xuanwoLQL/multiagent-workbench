import {
  ChevronDown,
  CircleStop,
  FolderPlus,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { Dashboard, Project } from "../api/types";
import { useUi } from "./store";
import { Button, IconButton, Kbd } from "../ui/primitives";
import { Popover } from "../ui/overlay";
import { shortPath } from "../lib/format";
import { useInvalidateProject } from "./queries";
import { useToast } from "../ui/toast";

export function TopBar({
  projects,
  dashboard,
}: {
  projects: Project[];
  dashboard?: Dashboard;
}) {
  const client = useQueryClient();
  const projectId = useUi((s) => s.projectId);
  const setProject = useUi((s) => s.setProject);
  const setCommandOpen = useUi((s) => s.setCommandOpen);
  const setNewTaskOpen = useUi((s) => s.setNewTaskOpen);
  const setView = useUi((s) => s.setView);
  const invalidate = useInvalidateProject(projectId);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const project = projects.find((item) => item.id === projectId);
  const running = Boolean(dashboard?.running);

  const toggleRun = async () => {
    if (!projectId) return;
    setBusy(true);
    try {
      await fetch(`/api/projects/${projectId}/${running ? "stop" : "run"}`, { method: "POST" });
      invalidate();
      toast.success(running ? "已请求停止调度" : "调度已启动");
    } catch (error) {
      toast.error("操作失败", (error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark">M</span>
        <span className="brand-name">MultiAgent</span>
      </div>
      <div className="topbar-divider" />
      <Popover
        width={300}
        trigger={({ toggle }) => (
          <button type="button" className="project-switch" onClick={toggle}>
            <span className="project-avatar">{project?.name?.slice(0, 1).toUpperCase() || "?"}</span>
            <span className="truncate">
              <span className="project-name">{project?.label || project?.name || "选择项目"}</span>
              <span className="project-path" style={{ display: "block" }}>
                {project?.root_path ? shortPath(project.root_path, 2) : "尚未打开工作区"}
              </span>
            </span>
            <ChevronDown size={14} className="muted" />
          </button>
        )}
      >
        {({ close }) => (
          <div className="menu">
            <div className="menu-label">项目</div>
            {projects.map((item) => (
              <button
                type="button"
                key={item.id}
                className="menu-item"
                onClick={() => {
                  setProject(item.id);
                  close();
                }}
              >
                <span className="project-avatar">{item.name.slice(0, 1).toUpperCase()}</span>
                <span className="menu-item-text">
                  <span>{item.label || item.name}</span>
                  <span className="menu-item-sub">{shortPath(item.root_path, 3)}</span>
                </span>
                {item.id === projectId ? <span className="menu-check">✓</span> : null}
              </button>
            ))}
            <div className="menu-separator" />
            <button
              type="button"
              className="menu-item"
              onClick={() => {
                setView("settings");
                close();
              }}
            >
              <FolderPlus size={15} />
              <span className="menu-item-text">
                <span>导入或新建项目</span>
                <span className="menu-item-sub">从本地目录开始</span>
              </span>
            </button>
          </div>
        )}
      </Popover>

      <div className="topbar-spacer" />
      <div className="topbar-actions">
        <button type="button" className="search-trigger" onClick={() => setCommandOpen(true)}>
          <Search size={14} />
          <span>搜索</span>
          <Kbd>Ctrl K</Kbd>
        </button>
        <IconButton
          label="刷新"
          onClick={() => client.invalidateQueries()}
          title="刷新数据"
        >
          <RefreshCw size={15} />
        </IconButton>
        {projectId ? (
          <Button
            size="sm"
            variant={running ? "danger" : "default"}
            loading={busy}
            onClick={() => void toggleRun()}
            title={running ? "停止当前调度" : "启动调度，把待执行任务派发给 Agent"}
          >
            {running ? <CircleStop size={14} /> : <Play size={14} />}
            {running ? "停止" : "开始运行"}
          </Button>
        ) : null}
        <Button size="sm" variant="primary" onClick={() => setNewTaskOpen(true)} disabled={!projectId}>
          <Plus size={14} /> 新任务
        </Button>
        <IconButton label="设置" onClick={() => setView("settings")}>
          <Settings2 size={16} />
        </IconButton>
      </div>
    </header>
  );
}
