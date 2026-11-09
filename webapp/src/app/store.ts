import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ViewKey =
  | "projects"
  | "overview"
  | "tasks"
  | "runs"
  | "files"
  | "review"
  | "memory"
  | "agents"
  | "settings";

interface UiState {
  projectId: string | null;
  view: ViewKey;
  taskId: string | null;
  taskTab: "overview" | "changes" | "validation" | "compare" | "activity" | "logs" | "context";
  commandOpen: boolean;
  newTaskOpen: boolean;
  sidebarCollapsed: boolean;
  theme: "light" | "dark" | "system";
  setProject: (id: string | null) => void;
  setView: (view: ViewKey) => void;
  openTask: (taskId: string, tab?: UiState["taskTab"]) => void;
  closeTask: () => void;
  setTaskTab: (tab: UiState["taskTab"]) => void;
  setCommandOpen: (open: boolean) => void;
  setNewTaskOpen: (open: boolean) => void;
  toggleSidebar: () => void;
  setTheme: (theme: UiState["theme"]) => void;
}

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      projectId: null,
      view: "overview",
      taskId: null,
      taskTab: "overview",
      commandOpen: false,
      newTaskOpen: false,
      sidebarCollapsed: false,
      theme: "system",
      setProject: (projectId) => set({ projectId, view: "overview", taskId: null }),
      setView: (view) => set({ view, taskId: null }),
      openTask: (taskId, tab) => set({ taskId, taskTab: tab ?? "overview" }),
      closeTask: () => set({ taskId: null }),
      setTaskTab: (taskTab) => set({ taskTab }),
      setCommandOpen: (commandOpen) => set({ commandOpen }),
      setNewTaskOpen: (newTaskOpen) => set({ newTaskOpen }),
      toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
      setTheme: (theme) => set({ theme }),
    }),
    {
      name: "multiagent.ui.v2",
      partialize: (state) => ({
        projectId: state.projectId,
        view: state.view,
        theme: state.theme,
        sidebarCollapsed: state.sidebarCollapsed,
      }),
    },
  ),
);
