import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, qk, wsProtocols, wsUrl } from "../api/client";
import type {
  AgentUsage,
  Dashboard,
  GitInfo,
  Project,
  StreamEvent,
  TaskDetail,
  TaskWorktree,
} from "../api/types";

export function useProjects() {
  return useQuery({ queryKey: qk.projects, queryFn: () => api.get<Project[]>("/api/projects") });
}

export function useDashboard(pid: string | null) {
  return useQuery({
    queryKey: qk.dashboard(pid ?? ""),
    queryFn: () => api.get<Dashboard>(`/api/projects/${pid}/dashboard`),
    enabled: Boolean(pid),
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });
}

export function useTask(tid: string | null) {
  return useQuery({
    queryKey: qk.task(tid ?? ""),
    queryFn: () => api.get<TaskDetail>(`/api/tasks/${tid}`),
    enabled: Boolean(tid),
    refetchInterval: (query) =>
      query.state.data?.task?.status === "running" ? 3000 : 15000,
  });
}

export function useAgents() {
  return useQuery({
    queryKey: qk.agentDetail,
    queryFn: () => api.get<AgentUsage[]>("/api/agents/detail"),
    refetchInterval: 30000,
  });
}

export function useGit(pid: string | null) {
  return useQuery({
    queryKey: qk.git(pid ?? ""),
    queryFn: () => api.get<GitInfo>(`/api/projects/${pid}/git`),
    enabled: Boolean(pid),
    retry: false,
  });
}

export function useTaskWorktree(tid: string | null, enabled = true) {
  return useQuery({
    queryKey: ["worktree", tid ?? ""],
    queryFn: () => api.get<TaskWorktree>(`/api/tasks/${tid}/worktree`),
    enabled: Boolean(tid) && enabled,
    retry: false,
  });
}

export function useInvalidateProject(pid: string | null) {
  const client = useQueryClient();
  return useCallback(() => {
    if (!pid) return;
    void client.invalidateQueries({ queryKey: qk.dashboard(pid) });
    void client.invalidateQueries({ queryKey: qk.projects });
  }, [client, pid]);
}

export function useLiveProject(pid: string | null) {
  const client = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);

  useEffect(() => {
    if (!pid) return;
    let closed = false;
    let timer: number | undefined;

    const connect = () => {
      if (closed) return;
      const socket = new WebSocket(wsUrl(pid), wsProtocols());
      socketRef.current = socket;
      socket.onopen = () => {
        retryRef.current = 0;
        setConnected(true);
      };
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(String(message.data)) as StreamEvent & {
            type: string;
            items?: StreamEvent[];
            event?: StreamEvent;
          };
          if (payload.type === "stream.event" && payload.event) {
            setEvents((prev) => [...prev.slice(-600), payload.event as StreamEvent]);
          } else if (payload.type === "stream.snapshot" && payload.items) {
            setEvents(payload.items.slice(-600));
          } else if (
            payload.type === "events" ||
            payload.type === "progress" ||
            payload.type === "run_status"
          ) {
            void client.invalidateQueries({ queryKey: qk.dashboard(pid) });
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        socketRef.current = null;
        if (closed) return;
        const delay = Math.min(8000, 500 * 2 ** retryRef.current++);
        timer = window.setTimeout(connect, delay);
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [pid, client]);

  return { connected, events, clear: () => setEvents([]) };
}

export function useDashboardActions(pid: string | null) {
  const client = useQueryClient();
  const invalidate = useInvalidateProject(pid);
  const wrap = <T,>(fn: () => Promise<T>) => async () => {
    const result = await fn();
    invalidate();
    return result;
  };
  return {
    start: useMutation({
      mutationFn: wrap(() => api.post(`/api/projects/${pid}/run`, {})),
      onSuccess: invalidate,
    }),
    stop: useMutation({
      mutationFn: wrap(() => api.post(`/api/projects/${pid}/stop`, {})),
      onSuccess: invalidate,
    }),
    recover: useMutation({
      mutationFn: wrap(() => api.post(`/api/projects/${pid}/recover`, {})),
      onSuccess: invalidate,
    }),
    runTask: useMutation({
      mutationFn: (tid: string) => api.post(`/api/tasks/${tid}/run`, {}),
      onSuccess: (_, tid) => {
        invalidate();
        void client.invalidateQueries({ queryKey: qk.task(tid) });
      },
    }),
    taskOp: useMutation({
      mutationFn: ({ tid, op, body }: { tid: string; op: string; body?: unknown }) =>
        api.post(`/api/tasks/${tid}/${op}`, body ?? {}),
      onSuccess: (_, { tid }) => {
        invalidate();
        void client.invalidateQueries({ queryKey: qk.task(tid) });
      },
    }),
  };
}
