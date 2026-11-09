import { Suspense, lazy, useEffect, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, FolderOpen, Plus, RefreshCw } from "lucide-react";
import { TopBar } from "./TopBar";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { useUi } from "./store";
import { useAgents, useDashboard, useGit, useLiveProject, useProjects } from "./queries";
import { WelcomeView } from "../features/onboarding/WelcomeView";
import { CommandPalette } from "./CommandPalette";
import { NewTaskDialog } from "../features/tasks/NewTaskDialog";
import { Button } from "../ui/primitives";

const OverviewView = lazy(() =>
  import("../features/overview/OverviewView").then((module) => ({ default: module.OverviewView })),
);
const TasksView = lazy(() =>
  import("../features/tasks/TasksView").then((module) => ({ default: module.TasksView })),
);
const TaskDetailView = lazy(() =>
  import("../features/task/TaskDetailView").then((module) => ({ default: module.TaskDetailView })),
);
const RunsView = lazy(() =>
  import("../features/runs/RunsView").then((module) => ({ default: module.RunsView })),
);
const FilesView = lazy(() =>
  import("../features/files/FilesView").then((module) => ({ default: module.FilesView })),
);
const ReviewView = lazy(() =>
  import("../features/review/ReviewView").then((module) => ({ default: module.ReviewView })),
);
const MemoryView = lazy(() =>
  import("../features/memory/MemoryView").then((module) => ({ default: module.MemoryView })),
);
const AgentsView = lazy(() =>
  import("../features/agents/AgentsView").then((module) => ({ default: module.AgentsView })),
);
const SettingsView = lazy(() =>
  import("../features/settings/SettingsView").then((module) => ({ default: module.SettingsView })),
);
const ProjectsView = lazy(() =>
  import("../features/projects/ProjectsView").then((module) => ({ default: module.ProjectsView })),
);

function useTheme() {
  const theme = useUi((state) => state.theme);
  useEffect(() => {
    const apply = () => {
      const dark =
        theme === "dark" ||
        (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    };
    apply();
    if (theme !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
}

export default function App() {
  useTheme();
  const client = useQueryClient();
  const projectId = useUi((state) => state.projectId);
  const view = useUi((state) => state.view);
  const taskId = useUi((state) => state.taskId);
  const setProject = useUi((state) => state.setProject);
  const setView = useUi((state) => state.setView);
  const openTask = useUi((state) => state.openTask);

  const projects = useProjects();
  const dashboard = useDashboard(projectId);
  const agents = useAgents();
  const git = useGit(projectId);
  const live = useLiveProject(projectId);

  const projectList = projects.data ?? [];
  const validProject = useMemo(
    () => projectList.find((item) => item.id === projectId) ?? null,
    [projectList, projectId],
  );

  useEffect(() => {
    if (!projects.isSuccess) return;
    if (validProject) return;
    setProject(projectList[0]?.id ?? null);
  }, [projects.isSuccess, projectList, validProject, setProject]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        useUi.getState().setCommandOpen(true);
      }
      if (event.key === "Escape" && !useUi.getState().commandOpen) {
        if (useUi.getState().taskId) useUi.getState().closeTask();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (projects.isLoading) {
    return (
      <div className="boot">
        <div className="boot-mark">M</div>
        <div className="boot-text">正在加载工作台…</div>
      </div>
    );
  }

  if (projects.isError) {
    return (
      <div className="boot">
        <AlertTriangle size={22} />
        <div className="boot-text">无法连接本地服务</div>
        <div className="boot-hint">{(projects.error as Error)?.message}</div>
        <Button onClick={() => client.invalidateQueries()}>
          <RefreshCw size={14} /> 重试
        </Button>
      </div>
    );
  }

  if (!projectList.length) {
    return (
      <>
        <WelcomeView
          onImported={(id) => {
            void client.invalidateQueries({ queryKey: ["projects"] });
            setProject(id);
          }}
        />
      </>
    );
  }

  const content = (() => {
    if (view === "projects") return <ProjectsView />;
    if (view === "settings") return <SettingsView />;
    if (view === "agents") return <AgentsView />;
    if (view === "files") return <FilesView />;
    if (view === "review") return <ReviewView />;
    if (view === "memory") return <MemoryView />;
    if (view === "runs") return <RunsView onOpenTask={(id) => openTask(id)} />;
    if (view === "tasks") {
      return taskId ? (
        <TaskDetailView taskId={taskId} onBack={() => setView("tasks")} />
      ) : (
        <TasksView onOpenTask={(id) => openTask(id)} />
      );
    }
    return (
      <OverviewView
        onOpenTask={(id, tab) => openTask(id, tab)}
        onNewTask={() => useUi.getState().setNewTaskOpen(true)}
        onViewTasks={() => setView("tasks")}
      />
    );
  })();

  return (
    <div className="app">
      <TopBar projects={projectList} dashboard={dashboard.data} />
      <div className="shell">
        <Sidebar dashboard={dashboard.data} agents={dashboard.data?.agents ?? agents.data} />
        <main className="main" key={`${projectId}-${view}`}>
          <Suspense fallback={<ViewSkeleton />}>{content}</Suspense>
        </main>
      </div>
      <StatusBar
        connected={live.connected}
        dashboard={dashboard.data}
        branch={git.data?.is_repo ? git.data.branch : undefined}
      />
      <CommandPalette />
      <NewTaskDialog />
    </div>
  );
}

function ViewSkeleton() {
  return (
    <div className="page">
      <div className="skeleton" style={{ width: 220, height: 26 }} />
      <div className="skeleton" style={{ width: 360, height: 13, marginTop: 10 }} />
      <div className="skeleton" style={{ height: 240, marginTop: 22 }} />
    </div>
  );
}
