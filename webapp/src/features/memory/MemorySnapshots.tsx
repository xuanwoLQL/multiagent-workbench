import { useEffect, useId, useMemo, useState } from "react";
import { Camera, Plus, RotateCcw } from "lucide-react";
import { fmtBytes, fmtDateTime, fmtInt, fmtRelative } from "../../lib/format";
import { Badge, Button, EmptyState, Field, Input } from "../../ui/primitives";
import { ConfirmDialog, Dialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import {
  useCreateSnapshot,
  useRestoreSnapshot,
  useSnapshots,
  type SnapshotRecord,
} from "./memory-api";
import { LoadError, shortId, SkeletonRows } from "./memory-shared";

interface SnapshotTabProps {
  projectId: string;
  query: string;
  onClearSearch: () => void;
}

function snapshotKindLabel(kind?: string): string {
  if (kind === "auto") return "自动";
  if (kind === "full") return "全量";
  return kind || "全量";
}

function matchesQuery(snapshot: SnapshotRecord, needle: string): boolean {
  if (!needle) return true;
  return `${snapshot.label ?? ""} ${snapshot.id} ${snapshot.kind ?? ""} ${snapshotKindLabel(snapshot.kind)}`
    .toLowerCase()
    .includes(needle);
}

export function MemorySnapshots({
  projectId,
  query,
  onClearSearch,
}: SnapshotTabProps) {
  const snapshots = useSnapshots(projectId);
  const restore = useRestoreSnapshot();
  const toast = useToast();
  const [createOpen, setCreateOpen] = useState(false);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const needle = query.trim().toLowerCase();

  const rows = useMemo(
    () =>
      (snapshots.data ?? []).filter((snapshot) =>
        matchesQuery(snapshot, needle),
      ),
    [snapshots.data, needle],
  );

  const selected = (snapshots.data ?? []).find(
    (snapshot) => snapshot.id === confirmId,
  );
  const total = snapshots.data?.length ?? 0;

  const doRestore = async () => {
    if (!confirmId || restore.isPending) return;
    try {
      const result = await restore.mutateAsync({ id: confirmId });
      setConfirmId(null);
      toast.success(
        "已恢复版本快照",
        result.pre_snapshot
          ? `恢复前已自动创建安全快照 ${shortId(result.pre_snapshot)}`
          : `已恢复 ${fmtInt(result.files)} 个文件`,
      );
    } catch (error) {
      toast.error("恢复失败", (error as Error).message);
    }
  };

  return (
    <div className="stack">
      <div className="memory-section-head">
        <div>
          <h2>版本快照</h2>
          <div className="memory-section-sub">
            保存工作区全量副本与哈希清单，随时可回退到某个稳定现场。
          </div>
        </div>
        <div className="memory-section-actions">
          <Badge tone="outline">{rows.length} 个快照</Badge>
          <Button variant="primary" onClick={() => setCreateOpen(true)}>
            <Plus size={14} />
            新建快照
          </Button>
        </div>
      </div>

      {snapshots.isLoading ? (
        <SkeletonRows count={4} />
      ) : snapshots.isError ? (
        <LoadError
          title="无法读取版本快照"
          message={(snapshots.error as Error)?.message}
          onRetry={() => void snapshots.refetch()}
        />
      ) : rows.length === 0 ? (
        <div className="memory-empty-panel">
          <EmptyState
            icon={<Camera size={18} />}
            title={needle && total > 0 ? "没有匹配的快照" : "还没有版本快照"}
            hint={
              needle && total > 0
                ? "换个关键词或快照 ID，或者清除搜索查看全部。"
                : "在重大改动前创建快照，可随时恢复工作区。恢复前系统还会自动保存当前状态。"
            }
            action={
              needle && total > 0 ? (
                <Button size="sm" onClick={onClearSearch}>
                  清除搜索
                </Button>
              ) : (
                <Button size="sm" onClick={() => setCreateOpen(true)}>
                  创建第一个快照
                </Button>
              )
            }
          />
        </div>
      ) : (
        <div className="memory-snapshot-list">
          {rows.map((snapshot) => (
            <div key={snapshot.id} className="memory-snapshot-row">
              <div className="memory-snapshot-main">
                <div className="memory-snapshot-title">
                  <span>{snapshot.label?.trim() || "未命名快照"}</span>
                  <Badge
                    tone={snapshot.kind === "auto" ? "warning" : "outline"}
                  >
                    {snapshotKindLabel(snapshot.kind)}
                  </Badge>
                </div>
                <div className="memory-snapshot-meta">
                  <span title={snapshot.created_at}>
                    {fmtDateTime(snapshot.created_at)}
                  </span>
                  <span>{fmtRelative(snapshot.created_at)}</span>
                  {typeof snapshot.file_count === "number" ? (
                    <span>{fmtInt(snapshot.file_count)} 个文件</span>
                  ) : null}
                  {typeof snapshot.bytes === "number" ? (
                    <span>{fmtBytes(snapshot.bytes)}</span>
                  ) : null}
                </div>
              </div>
              <div className="memory-snapshot-actions">
                <span className="memory-snapshot-id" title={snapshot.id}>
                  {shortId(snapshot.id)}
                </span>
                <Button
                  size="sm"
                  onClick={() => setConfirmId(snapshot.id)}
                >
                  <RotateCcw size={12} />
                  恢复
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {needle && rows.length > 0 ? (
        <div className="memory-list-foot">
          正在显示 {rows.length} / {total} 个快照
        </div>
      ) : null}

      <SnapshotCreateDialog
        projectId={projectId}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
      />

      <ConfirmDialog
        open={Boolean(confirmId)}
        title="恢复版本快照"
        danger
        confirmLabel={restore.isPending ? "正在恢复…" : "恢复到此快照"}
        onCancel={() => {
          if (!restore.isPending) setConfirmId(null);
        }}
        onConfirm={() => void doRestore()}
        message={
          selected ? (
            <div>
              <p>
                将把当前工作区恢复为{" "}
                <strong>{selected.label?.trim() || shortId(selected.id)}</strong>
                （{fmtInt(selected.file_count)} 个文件，{fmtBytes(selected.bytes)}）。
              </p>
              <p className="memory-warning-text">
                当前未提交的改动会被覆盖。系统会先自动创建一份恢复前快照，便于再次回退。
              </p>
            </div>
          ) : (
            "确认恢复这个版本快照？"
          )
        }
      />
    </div>
  );
}

function SnapshotCreateDialog({
  projectId,
  open,
  onClose,
}: {
  projectId: string;
  open: boolean;
  onClose: () => void;
}) {
  const toast = useToast();
  const create = useCreateSnapshot(projectId);
  const [label, setLabel] = useState("");
  const labelId = useId();

  useEffect(() => {
    if (open) setLabel("");
  }, [open]);

  const close = () => {
    if (!create.isPending) onClose();
  };

  const submit = async () => {
    if (!label.trim()) {
      toast.error("先填写快照标签");
      return;
    }
    try {
      await create.mutateAsync({ label: label.trim() });
      toast.success("已创建版本快照", label.trim());
      onClose();
    } catch (error) {
      toast.error("创建失败", (error as Error).message);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={close}
      title="新建版本快照"
      description="保存当前工作区的全量副本与哈希清单。"
      width={460}
      footer={
        <>
          <Button onClick={close} disabled={create.isPending}>
            取消
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            loading={create.isPending}
          >
            创建快照
          </Button>
        </>
      }
    >
      <Field
        label="快照标签"
        htmlFor={labelId}
        hint="建议写明阶段，例如「重构前」「提交验收前」。"
      >
        <Input
          id={labelId}
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          placeholder="例如：重构前"
          autoFocus
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void submit();
            }
          }}
        />
      </Field>
    </Dialog>
  );
}
