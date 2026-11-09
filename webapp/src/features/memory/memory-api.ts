import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";

export interface BoardEntry {
  id: string;
  project_id: string;
  task_id?: string | null;
  agent_id?: string | null;
  run_id?: string | null;
  scope: string;
  type: string;
  title: string;
  body: string;
  tags: string[];
  status: string;
  pinned: boolean;
  current_version: number;
  created_at: string;
  updated_at: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
}

export interface BoardVersion {
  id?: number;
  entry_id: string;
  version: number;
  title: string;
  body: string;
  author?: string | null;
  task_id?: string | null;
  run_id?: string | null;
  note?: string | null;
  at: string;
}

export interface BoardDetail {
  entry: BoardEntry;
  versions: BoardVersion[];
}

export interface HandoffRecord {
  id: string;
  project_id: string;
  task_id: string;
  run_id?: string | null;
  agent_id?: string | null;
  did?: string;
  did_not?: string;
  not_done?: string;
  open_issues?: unknown;
  next_steps?: unknown;
  artifacts?: unknown;
  notes?: string;
  source?: string;
  created_at?: string;
  edited_by?: string | null;
  edited_at?: string | null;
}

export interface ApprovalRecord {
  id: string;
  project_id: string;
  task_id?: string | null;
  run_id?: string | null;
  agent_id?: string | null;
  kind: string;
  title: string;
  detail?: Record<string, unknown> | string | null;
  risk?: string;
  status: string;
  created_at?: string;
  resolved_at?: string | null;
  resolved_by?: string | null;
  resolution?: string | null;
}

export interface SnapshotRecord {
  id: string;
  project_id: string;
  label?: string;
  kind?: string;
  store_path?: string;
  file_count?: number;
  bytes?: number;
  created_at?: string;
  parent_id?: string | null;
}

export interface SnapshotRestoreResult {
  snapshot_id: string;
  pre_snapshot?: string | null;
  files?: number;
}

export const memoryKeys = {
  all: ["memory"] as const,
  board: (pid: string, type: string, q: string) =>
    ["memory", "board", pid, type, q] as const,
  boardDetail: (id: string) => ["memory", "board", "detail", id] as const,
  handoffs: (pid: string) => ["memory", "handoffs", pid] as const,
  approvals: (pid: string) => ["memory", "approvals", pid] as const,
  snapshots: (pid: string) => ["memory", "snapshots", pid] as const,
};

function invalidateMemory(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: ["memory"] });
  void client.invalidateQueries({ queryKey: ["dashboard"] });
}

export function useBoard(projectId: string, type: string, q: string) {
  const needle = q.trim();
  return useQuery({
    queryKey: memoryKeys.board(projectId, type, needle),
    queryFn: () => {
      const params = new URLSearchParams();
      if (type !== "all") params.set("type", type);
      if (needle) params.set("q", needle);
      const suffix = params.toString();
      return api.get<BoardEntry[]>(
        `/api/projects/${projectId}/board${suffix ? `?${suffix}` : ""}`,
      );
    },
    enabled: Boolean(projectId),
    refetchInterval: 15000,
    retry: false,
  });
}

export function useBoardDetail(entryId: string | null) {
  return useQuery({
    queryKey: memoryKeys.boardDetail(entryId ?? ""),
    queryFn: () => api.get<BoardDetail>(`/api/board/${entryId}`),
    enabled: Boolean(entryId),
    retry: false,
  });
}

export function useCreateBoardEntry(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { type: string; title: string; body: string }) =>
      api.post<BoardEntry>(`/api/projects/${projectId}/board`, {
        type: input.type,
        title: input.title,
        body: input.body,
        scope: "project",
      }),
    onSuccess: () => invalidateMemory(client),
  });
}

export function useReviewBoardEntry() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { id: string; approved: boolean; note?: string }) =>
      api.post<BoardEntry>(`/api/board/${input.id}/review`, {
        approved: input.approved,
        reviewer: "human",
        note: input.note || undefined,
      }),
    onSuccess: (entry) => {
      invalidateMemory(client);
      void client.invalidateQueries({ queryKey: memoryKeys.boardDetail(entry.id) });
    },
  });
}

export function useHandoffs(projectId: string) {
  return useQuery({
    queryKey: memoryKeys.handoffs(projectId),
    queryFn: () => api.get<HandoffRecord[]>(`/api/projects/${projectId}/handoffs`),
    enabled: Boolean(projectId),
    refetchInterval: 20000,
    retry: false,
  });
}

export function useEditHandoff() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { id: string; notes: string }) =>
      api.patch<HandoffRecord>(`/api/handoffs/${input.id}`, {
        notes: input.notes,
        editor: "human",
      }),
    onSuccess: () => invalidateMemory(client),
  });
}

export function useApprovals(projectId: string) {
  return useQuery({
    queryKey: memoryKeys.approvals(projectId),
    queryFn: () => api.get<ApprovalRecord[]>(`/api/projects/${projectId}/approvals`),
    enabled: Boolean(projectId),
    refetchInterval: 10000,
    retry: false,
  });
}

export function useResolveApproval() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      id: string;
      decision: "approve" | "reject";
      note?: string;
    }) =>
      api.post<ApprovalRecord>(`/api/approvals/${input.id}/resolve`, {
        decision: input.decision,
        note: input.note || undefined,
        approved: input.decision === "approve",
        resolution: input.note || undefined,
        by: "human",
      }),
    onSuccess: () => {
      invalidateMemory(client);
      void client.invalidateQueries({ queryKey: ["task"] });
    },
  });
}

export function useSnapshots(projectId: string) {
  return useQuery({
    queryKey: memoryKeys.snapshots(projectId),
    queryFn: () => api.get<SnapshotRecord[]>(`/api/projects/${projectId}/snapshots`),
    enabled: Boolean(projectId),
    refetchInterval: 30000,
    retry: false,
  });
}

export function useCreateSnapshot(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { label: string }) =>
      api.post<SnapshotRecord>(`/api/projects/${projectId}/snapshots`, {
        label: input.label,
      }),
    onSuccess: () => invalidateMemory(client),
  });
}

export function useRestoreSnapshot() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { id: string }) =>
      api.post<SnapshotRestoreResult>(`/api/snapshots/${input.id}/restore`, {}),
    onSuccess: () => {
      invalidateMemory(client);
      void client.invalidateQueries({ queryKey: ["git"] });
      void client.invalidateQueries({ queryKey: ["files"] });
      void client.invalidateQueries({ queryKey: ["task"] });
    },
  });
}
