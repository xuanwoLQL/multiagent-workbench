import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  FolderTree,
  GitPullRequest,
  LayoutDashboard,
  ListChecks,
  Play,
  Plus,
  Search,
  Settings,
  Square,
} from "lucide-react";
import clsx from "clsx";
import { useDashboard, useProjects } from "./queries";
import { useUi, type ViewKey } from "./store";
import { Kbd } from "../ui/primitives";
import { Dialog } from "../ui/overlay";
import { useToast } from "../ui/toast";

interface Command {
  id: string;
  label: string;
  hint?: string;
  icon: typeof Search;
  run: () => void;
  keywords?: string;
}

export function CommandPalette() {
  const open = useUi((state) => state.commandOpen);
  const setOpen = useUi((state) => state.setCommandOpen);
  const setView = useUi((state) => state.setView);
  const openTask = useUi((state) => state.openTask);
  const setProject = useUi((state) => state.setProject);
  const setNewTaskOpen = useUi((state) => state.setNewTaskOpen);
  const projectId = useUi((state) => state.projectId);
  const projects = useProjects();
  const dashboard = useDashboard(projectId);
  const invalidate = useQueryClient();
  const toast = useToast();

  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setIndex(0);
    }
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const nav: Array<{ view: ViewKey; label: string; icon: typeof Search }> = [
      { view: "overview", label: "打开概览", icon: LayoutDashboard },
      { view: "tasks", label: "打开任务", icon: ListChecks },
      { view: "runs", label: "打开运行记录", icon: Activity },
      { view: "files", label: "打开文件", icon: FolderTree },
      { view: "review", label: "打开审阅", icon: GitPullRequest },
      { view: "agents", label: "打开 Agent", icon: Bot },
      { view: "settings", label: "打开设置", icon: Settings },
    ];
    const actions: Command[] = [
      {
        id: "new-task",
        label: "新建任务",
        hint: "T",
        icon: Plus,
        keywords: "new task create 创建",
        run: () => {
          setNewTaskOpen(true);
          setOpen(false);
        },
      },
    ];
    if (projectId) {
      actions.push({
        id: "start",
        label: dashboard.data?.running ? "停止调度" : "开始运行队列",
        icon: dashboard.data?.running ? Square : Play,
        keywords: "run start stop 运行 停止",
        run: async () => {
          await fetch(`/api/projects/${projectId}/${dashboard.data?.running ? "stop" : "run"}`, {
            method: "POST",
          });
          void invalidate.invalidateQueries();
          toast.success(dashboard.data?.running ? "已请求停止" : "调度已启动");
          setOpen(false);
        },
      });
    }
    const navigation: Command[] = nav.map((item) => ({
      id: `nav-${item.view}`,
      label: item.label,
      icon: item.icon,
      keywords: item.view,
      run: () => {
        setView(item.view);
        setOpen(false);
      },
    }));
    const projectCommands: Command[] = (projects.data ?? []).map((project) => ({
      id: `project-${project.id}`,
      label: `切换到 ${project.label || project.name}`,
      hint: project.root_path,
      icon: LayoutDashboard,
      keywords: `project ${project.name} ${project.root_path}`,
      run: () => {
        setProject(project.id);
        setOpen(false);
      },
    }));
    const taskCommands: Command[] = (dashboard.data?.tasks ?? []).slice(0, 40).map((task) => ({
      id: `task-${task.id}`,
      label: task.title,
      hint: task.status,
      icon: ListChecks,
      keywords: `${task.title} ${task.kind} ${task.status}`,
      run: () => {
        setView("tasks");
        openTask(task.id);
        setOpen(false);
      },
    }));
    return [...actions, ...navigation, ...projectCommands, ...taskCommands];
  }, [
    dashboard.data,
    invalidate,
    openTask,
    projectId,
    projects.data,
    setNewTaskOpen,
    setOpen,
    setProject,
    setView,
    toast,
  ]);

  const filtered = useMemo(() => {
    const value = query.trim().toLowerCase();
    if (!value) return commands;
    return commands
      .map((command) => {
        const haystack = `${command.label} ${command.hint ?? ""} ${command.keywords ?? ""}`.toLowerCase();
        const position = haystack.indexOf(value);
        return { command, score: position < 0 ? 999 : position };
      })
      .filter((item) => item.score < 999)
      .sort((a, b) => a.score - b.score)
      .map((item) => item.command)
      .slice(0, 40);
  }, [commands, query]);

  useEffect(() => {
    setIndex(0);
  }, [query]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setIndex((value) => Math.min(filtered.length - 1, value + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setIndex((value) => Math.max(0, value - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      filtered[index]?.run();
    }
  };

  return (
    <Dialog
      open={open}
      onClose={() => setOpen(false)}
      title="搜索与命令"
      width={620}
      className="command-dialog"
    >
      <div className="command">
        <div className="command-input">
          <Search size={15} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="搜索任务、项目或执行命令"
            autoFocus
          />
          <Kbd>Esc</Kbd>
        </div>
        <div className="command-list" ref={listRef}>
          {filtered.length ? (
            filtered.map((command, itemIndex) => {
              const Icon = command.icon;
              return (
                <button
                  type="button"
                  key={command.id}
                  className={clsx("command-item", itemIndex === index && "is-active")}
                  onMouseEnter={() => setIndex(itemIndex)}
                  onClick={() => command.run()}
                >
                  <Icon size={15} />
                  <span className="truncate">{command.label}</span>
                  {command.hint ? <span className="command-hint truncate">{command.hint}</span> : null}
                  {itemIndex === index ? <span className="command-enter">↵</span> : null}
                </button>
              );
            })
          ) : (
            <div className="command-empty">没有匹配的命令</div>
          )}
        </div>
      </div>
    </Dialog>
  );
}
