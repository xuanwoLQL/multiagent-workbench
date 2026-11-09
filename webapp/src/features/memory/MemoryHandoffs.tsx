import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ClipboardList, Pencil, Save } from "lucide-react";
import { fmtDateTime, fmtRelative } from "../../lib/format";
import { Badge, Button, EmptyState, Textarea } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import {
  useEditHandoff,
  useHandoffs,
  type HandoffRecord,
} from "./memory-api";
import { asList, LoadError, shortId, SkeletonRows } from "./memory-shared";

interface HandoffTabProps {
  projectId: string;
  query: string;
  onClearSearch: () => void;
}

function handoffHaystack(record: HandoffRecord): string {
  return [
    record.task_id,
    record.agent_id ?? "",
    record.did ?? "",
    record.not_done ?? record.did_not ?? "",
    record.notes ?? "",
    ...asList(record.open_issues),
    ...asList(record.next_steps),
    ...asList(record.artifacts),
  ]
    .join(" ")
    .toLowerCase();
}

export function MemoryHandoffs({
  projectId,
  query,
  onClearSearch,
}: HandoffTabProps) {
  const handoffs = useHandoffs(projectId);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const needle = query.trim().toLowerCase();

  const rows = useMemo(
    () =>
      (handoffs.data ?? []).filter(
        (record) => !needle || handoffHaystack(record).includes(needle),
      ),
    [handoffs.data, needle],
  );
  const total = handoffs.data?.length ?? 0;

  return (
    <div className="stack">
      <div className="memory-section-head">
        <div>
          <h2>交接说明</h2>
          <div className="memory-section-sub">
            每个任务结束时自动生成：做了什么、没做什么、遗留问题与下一步建议。
          </div>
        </div>
        <div className="memory-section-actions">
          <Badge tone="outline">{rows.length} 条交接</Badge>
        </div>
      </div>

      {handoffs.isLoading ? (
        <SkeletonRows count={3} />
      ) : handoffs.isError ? (
        <LoadError
          title="无法读取交接说明"
          message={(handoffs.error as Error)?.message}
          onRetry={() => void handoffs.refetch()}
        />
      ) : rows.length === 0 ? (
        <div className="memory-empty-panel">
          <EmptyState
            icon={<ClipboardList size={18} />}
            title={needle && total > 0 ? "没有匹配的交接说明" : "还没有交接说明"}
            hint={
              needle && total > 0
                ? "换个关键词，或清除搜索查看全部交接。"
                : "任务结束时会自动写入交接。人工修正后的备注会成为后续任务优先采用的上下文。"
            }
            action={
              needle && total > 0 ? (
                <Button size="sm" onClick={onClearSearch}>
                  清除搜索
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : (
        <div className="memory-handoff-list">
          {rows.map((record, index) => {
            const open = expanded[record.id] ?? index === 0;
            return (
              <HandoffCard
                key={record.id}
                record={record}
                open={open}
                onToggle={() =>
                  setExpanded((prev) => ({
                    ...prev,
                    [record.id]: !(prev[record.id] ?? index === 0),
                  }))
                }
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function HandoffCard({
  record,
  open,
  onToggle,
}: {
  record: HandoffRecord;
  open: boolean;
  onToggle: () => void;
}) {
  const toast = useToast();
  const edit = useEditHandoff();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(record.notes ?? "");

  const did = (record.did ?? "").trim();
  const notDone = (record.not_done ?? record.did_not ?? "").trim();
  const issues = asList(record.open_issues);
  const steps = asList(record.next_steps);

  useEffect(() => {
    if (!editing) setDraft(record.notes ?? "");
  }, [record.notes, editing]);

  const save = async () => {
    try {
      await edit.mutateAsync({ id: record.id, notes: draft });
      toast.success("交接备注已保存", shortId(record.task_id));
      setEditing(false);
    } catch (error) {
      toast.error("保存失败", (error as Error).message);
    }
  };

  return (
    <article className="card memory-handoff-card">
      <button
        type="button"
        className="memory-handoff-head"
        aria-expanded={open}
        onClick={onToggle}
      >
        <div className="memory-handoff-head-main">
          <div className="row memory-badge-row">
            <Badge tone="info" dot>
              交接
            </Badge>
            <Badge tone="outline">任务 {shortId(record.task_id)}</Badge>
            {record.agent_id ? <Badge tone="outline">{record.agent_id}</Badge> : null}
            {record.source === "human" || record.edited_by ? (
              <Badge tone="warning">人工修正</Badge>
            ) : null}
          </div>
          <div className="memory-handoff-summary">
            {did || record.notes || "（没有填写交接摘要）"}
          </div>
        </div>
        <div className="memory-handoff-head-side">
          <span className="memory-time">{fmtRelative(record.created_at)}</span>
          <ChevronDown
            size={15}
            className={`memory-handoff-chevron${open ? " is-open" : ""}`}
          />
        </div>
      </button>

      {open ? (
        <div className="memory-handoff-body">
          <div className="memory-handoff-grid">
            <section className="memory-handoff-section">
              <div className="memory-handoff-label">做了什么</div>
              <div className={`memory-handoff-text${did ? "" : " is-empty"}`}>
                {did || "（未填写）"}
              </div>
            </section>
            <section className="memory-handoff-section">
              <div className="memory-handoff-label">没做什么</div>
              <div className={`memory-handoff-text${notDone ? "" : " is-empty"}`}>
                {notDone || "无"}
              </div>
            </section>
            <section className="memory-handoff-section">
              <div className="memory-handoff-label">遗留问题</div>
              {issues.length ? (
                <ul className="memory-bullet-list">
                  {issues.map((issue, index) => (
                    <li key={`${issue}-${index}`}>{issue}</li>
                  ))}
                </ul>
              ) : (
                <div className="memory-handoff-text is-empty">无</div>
              )}
            </section>
            <section className="memory-handoff-section">
              <div className="memory-handoff-label">下一步建议</div>
              {steps.length ? (
                <ul className="memory-bullet-list">
                  {steps.map((step, index) => (
                    <li key={`${step}-${index}`}>{step}</li>
                  ))}
                </ul>
              ) : (
                <div className="memory-handoff-text is-empty">无</div>
              )}
            </section>
          </div>

          <div className="memory-handoff-notes">
            <div className="row-between">
              <div className="memory-handoff-label is-flush">人工备注</div>
              {!editing ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setEditing(true)}
                >
                  <Pencil size={12} />
                  编辑备注
                </Button>
              ) : null}
            </div>
            {editing ? (
              <div className="memory-notes-editor">
                <Textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  rows={3}
                  placeholder="补充上下文、风险或后续接手建议…"
                  autoFocus
                  aria-label="交接备注"
                />
                <div className="memory-handoff-actions">
                  <Button
                    size="sm"
                    onClick={() => {
                      setEditing(false);
                      setDraft(record.notes ?? "");
                    }}
                    disabled={edit.isPending}
                  >
                    取消
                  </Button>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => void save()}
                    loading={edit.isPending}
                  >
                    <Save size={12} />
                    保存
                  </Button>
                </div>
              </div>
            ) : (
              <div className={`memory-handoff-text${record.notes ? "" : " is-empty"}`}>
                {record.notes || "尚未添加人工备注"}
              </div>
            )}
          </div>

          <div className="memory-handoff-foot">
            <span>创建 {fmtDateTime(record.created_at)}</span>
            {record.run_id ? <span>Run {shortId(record.run_id)}</span> : null}
            {record.edited_by ? (
              <span>
                由 {record.edited_by} 修正于 {fmtRelative(record.edited_at)}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </article>
  );
}
