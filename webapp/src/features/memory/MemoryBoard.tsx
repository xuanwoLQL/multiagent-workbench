import { useEffect, useId, useMemo, useState } from "react";
import { BookOpen, Pin, Plus } from "lucide-react";
import { fmtDateTime, fmtRelative, initials } from "../../lib/format";
import { Badge, Button, EmptyState, Field, Input, Select, Textarea } from "../../ui/primitives";
import { Dialog, Drawer } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import {
  useBoard,
  useBoardDetail,
  useCreateBoardEntry,
  useReviewBoardEntry,
  type BoardEntry,
} from "./memory-api";
import {
  boardStatusLabel,
  boardStatusTone,
  boardTypeLabel,
  boardTypeTone,
  LoadError,
  shortId,
  SkeletonGrid,
  useDebouncedValue,
} from "./memory-shared";

const TYPE_OPTIONS = [
  "brief",
  "decision",
  "issue",
  "reference",
  "note",
  "todo",
  "handoff",
  "artifact",
];

interface BoardTabProps {
  projectId: string;
  query: string;
  onClearSearch: () => void;
}

function matchesQuery(entry: BoardEntry, needle: string): boolean {
  if (!needle) return true;
  const haystack = [
    entry.title,
    entry.body,
    entry.type,
    boardTypeLabel(entry.type),
    entry.status,
    boardStatusLabel(entry.status),
    entry.agent_id ?? "",
    entry.task_id ?? "",
    entry.reviewed_by ?? "",
    ...(entry.tags ?? []),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

export function MemoryBoard({ projectId, query, onClearSearch }: BoardTabProps) {
  const [type, setType] = useState("all");
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const debouncedQuery = useDebouncedValue(query, 250);
  const board = useBoard(projectId, type, debouncedQuery);

  const entries = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (board.data ?? []).filter(
      (entry) =>
        (type === "all" || entry.type === type) && matchesQuery(entry, needle),
    );
  }, [board.data, query, type]);

  const total = board.data?.length ?? 0;
  const filtering = total > 0 && (Boolean(query.trim()) || type !== "all");

  return (
    <div className="stack">
      <div className="memory-section-head">
        <div>
          <h2>共享黑板</h2>
          <div className="memory-section-sub">
            记录决策、参考、问题与任务交接；项目级条目经人工审核后进入长期记忆。
          </div>
        </div>
        <div className="memory-section-actions">
          <Select
            value={type}
            onChange={(event) => setType(event.target.value)}
            aria-label="按条目类型筛选"
          >
            <option value="all">全部类型</option>
            {TYPE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {boardTypeLabel(option)}
              </option>
            ))}
          </Select>
          <Button variant="primary" onClick={() => setCreateOpen(true)}>
            <Plus size={14} />
            新建条目
          </Button>
        </div>
      </div>

      {board.isLoading ? (
        <SkeletonGrid />
      ) : board.isError ? (
        <LoadError
          title="无法读取共享黑板"
          message={(board.error as Error)?.message}
          onRetry={() => void board.refetch()}
        />
      ) : entries.length === 0 ? (
        <div className="memory-empty-panel">
          <EmptyState
            icon={<BookOpen size={18} />}
            title={filtering ? "没有匹配的黑板条目" : "共享黑板还是空的"}
            hint={
              filtering
                ? "换个关键词或类型，或者清除筛选查看全部条目。"
                : "把决策、参考和踩过的坑写下来。项目级条目会先进入人工审核，通过后成为权威上下文。"
            }
            action={
              filtering ? (
                <Button
                  size="sm"
                  onClick={() => {
                    setType("all");
                    onClearSearch();
                  }}
                >
                  清除筛选
                </Button>
              ) : (
                <Button size="sm" onClick={() => setCreateOpen(true)}>
                  新建第一条
                </Button>
              )
            }
          />
        </div>
      ) : (
        <div className="memory-board-grid">
          {entries.map((entry) => (
            <BoardCard
              key={entry.id}
              entry={entry}
              onOpen={() => setSelectedId(entry.id)}
            />
          ))}
        </div>
      )}

      <BoardCreateDialog
        projectId={projectId}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(id) => setSelectedId(id)}
      />
      <BoardDetailDrawer
        entryId={selectedId}
        onClose={() => setSelectedId(null)}
      />
    </div>
  );
}

function BoardCard({ entry, onOpen }: { entry: BoardEntry; onOpen: () => void }) {
  const author = entry.agent_id || entry.reviewed_by || "human";
  const excerpt = (entry.body ?? "").replace(/\s+/g, " ").trim();

  return (
    <button
      type="button"
      className="memory-board-card"
      onClick={onOpen}
      aria-label={`打开黑板条目：${entry.title || excerpt.slice(0, 40) || "无标题"}`}
    >
      <div className="memory-board-card-head">
        <div className="row memory-badge-row">
          <Badge tone={boardTypeTone(entry.type)}>{boardTypeLabel(entry.type)}</Badge>
          <Badge tone={boardStatusTone(entry.status)} dot>
            {boardStatusLabel(entry.status)}
          </Badge>
          {entry.pinned ? (
            <Badge tone="accent">
              <Pin size={10} /> 置顶
            </Badge>
          ) : null}
        </div>
        <span className="memory-time">{fmtRelative(entry.updated_at)}</span>
      </div>
      <div className="memory-board-title">{entry.title || "（无标题）"}</div>
      <div className={`memory-board-excerpt${excerpt ? "" : " is-empty"}`}>
        {excerpt || "没有正文"}
      </div>
      <div className="memory-board-meta">
        <span className="memory-avatar">{initials(author)}</span>
        <span className="truncate">{author}</span>
        <span className="memory-meta-dot" />
        <span className="truncate">
          {entry.task_id ? `任务 ${shortId(entry.task_id)}` : "项目级"}
        </span>
        <span className="memory-meta-spacer" />
        <span>v{entry.current_version ?? 1}</span>
      </div>
    </button>
  );
}

function BoardCreateDialog({
  projectId,
  open,
  onClose,
  onCreated,
}: {
  projectId: string;
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const toast = useToast();
  const [type, setType] = useState("note");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const typeId = useId();
  const titleId = useId();
  const bodyId = useId();
  const create = useCreateBoardEntry(projectId);

  useEffect(() => {
    if (!open) return;
    setType("note");
    setTitle("");
    setBody("");
  }, [open]);

  const close = () => {
    if (!create.isPending) onClose();
  };

  const submit = async () => {
    if (!title.trim() && !body.trim()) {
      toast.error("至少填写标题或正文");
      return;
    }
    try {
      const entry = await create.mutateAsync({
        type,
        title: title.trim(),
        body: body.trim(),
      });
      toast.success(
        "已提交到共享黑板",
        entry.status === "pending" ? "项目级条目正在等待人工审核" : "条目已生效",
      );
      onCreated(entry.id);
      onClose();
    } catch (error) {
      toast.error("提交失败", (error as Error).message);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={close}
      title="新建黑板条目"
      description="项目级记忆默认需要人工审核，通过后才会注入后续任务上下文。"
      width={620}
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
            提交审核
          </Button>
        </>
      }
    >
      <div className="memory-form">
        <Field label="条目类型" htmlFor={typeId}>
          <Select
            id={typeId}
            value={type}
            onChange={(event) => setType(event.target.value)}
          >
            {TYPE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {boardTypeLabel(option)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="标题" htmlFor={titleId} hint="一句话说清这条记忆要解决什么问题。">
          <Input
            id={titleId}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="例如：数据库迁移必须先跑 003 脚本"
            autoFocus
          />
        </Field>
        <Field label="正文" htmlFor={bodyId} hint="支持纯文本与换行；不要粘贴不可信的 HTML。">
          <Textarea
            id={bodyId}
            value={body}
            onChange={(event) => setBody(event.target.value)}
            rows={8}
            placeholder="写清背景、结论、证据与适用范围，方便后续任务直接引用。"
          />
        </Field>
      </div>
    </Dialog>
  );
}

function BoardDetailDrawer({
  entryId,
  onClose,
}: {
  entryId: string | null;
  onClose: () => void;
}) {
  const detail = useBoardDetail(entryId);
  const review = useReviewBoardEntry();
  const toast = useToast();
  const [note, setNote] = useState("");

  useEffect(() => {
    setNote("");
  }, [entryId]);

  const entry = detail.data?.entry;
  const versions = detail.data?.versions ?? [];
  const latestVersion = versions.length ? versions[versions.length - 1] : undefined;
  const author = entry?.agent_id || latestVersion?.author || entry?.reviewed_by || "human";

  const act = async (approved: boolean) => {
    if (!entry) return;
    try {
      await review.mutateAsync({
        id: entry.id,
        approved,
        note: note.trim() || undefined,
      });
      toast.success(
        approved ? "条目已通过" : "条目已驳回",
        entry.title || shortId(entry.id),
      );
    } catch (error) {
      toast.error("审核失败", (error as Error).message);
    }
  };

  return (
    <Drawer
      open={Boolean(entryId)}
      onClose={onClose}
      title="黑板条目"
      width={640}
      footer={
        entry ? (
          <div className="memory-drawer-foot">
            {entry.status === "pending" ? (
              <>
                <Input
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="审核备注（可选）"
                  aria-label="审核备注"
                />
                <Button
                  variant="danger"
                  onClick={() => void act(false)}
                  loading={review.isPending && review.variables?.approved === false}
                  disabled={review.isPending}
                >
                  驳回
                </Button>
                <Button
                  variant="primary"
                  onClick={() => void act(true)}
                  loading={review.isPending && review.variables?.approved === true}
                  disabled={review.isPending}
                >
                  通过
                </Button>
              </>
            ) : (
              <Button onClick={onClose}>关闭</Button>
            )}
          </div>
        ) : undefined
      }
    >
      {detail.isLoading ? (
        <SkeletonGrid count={2} />
      ) : detail.isError ? (
        <LoadError
          title="无法读取条目"
          message={(detail.error as Error)?.message}
          onRetry={() => void detail.refetch()}
        />
      ) : entry ? (
        <div className="memory-detail">
          <div className="memory-detail-title">{entry.title || "（无标题）"}</div>
          <div className="row memory-detail-badges">
            <Badge tone={boardTypeTone(entry.type)}>{boardTypeLabel(entry.type)}</Badge>
            <Badge tone={boardStatusTone(entry.status)} dot>
              {boardStatusLabel(entry.status)}
            </Badge>
            <Badge tone="outline">
              {entry.scope === "project"
                ? "项目级"
                : entry.scope === "task"
                  ? "任务级"
                  : entry.scope}
            </Badge>
            {entry.pinned ? (
              <Badge tone="accent">
                <Pin size={10} /> 置顶
              </Badge>
            ) : null}
          </div>

          <dl className="memory-detail-grid">
            <div>
              <dt>作者</dt>
              <dd>{author}</dd>
            </div>
            <div>
              <dt>任务</dt>
              <dd>{entry.task_id ? shortId(entry.task_id) : "项目级"}</dd>
            </div>
            <div>
              <dt>创建时间</dt>
              <dd>{fmtDateTime(entry.created_at)}</dd>
            </div>
            <div>
              <dt>更新时间</dt>
              <dd>{fmtRelative(entry.updated_at)}</dd>
            </div>
            {entry.reviewed_by ? (
              <div>
                <dt>审核人</dt>
                <dd>{entry.reviewed_by}</dd>
              </div>
            ) : null}
            {entry.reviewed_at ? (
              <div>
                <dt>审核时间</dt>
                <dd>{fmtDateTime(entry.reviewed_at)}</dd>
              </div>
            ) : null}
          </dl>

          <div className="memory-detail-label">正文</div>
          {entry.body ? (
            <pre className="memory-body">{entry.body}</pre>
          ) : (
            <div className="memory-handoff-text is-empty">没有正文</div>
          )}

          {entry.tags?.length ? (
            <div className="memory-tags">
              {entry.tags.map((tag) => (
                <Badge key={tag} tone="outline">
                  {tag}
                </Badge>
              ))}
            </div>
          ) : null}

          {versions.length ? (
            <div className="memory-versions-block">
              <div className="memory-detail-label">版本历史</div>
              <div className="memory-version-list">
                {versions
                  .slice()
                  .reverse()
                  .map((version) => (
                    <div key={version.version} className="memory-version">
                      <span className="memory-version-no">v{version.version}</span>
                      <span className="truncate">{version.author || "未知作者"}</span>
                      <span className="memory-time">{fmtRelative(version.at)}</span>
                      {version.note ? (
                        <span className="memory-version-note">{version.note}</span>
                      ) : null}
                    </div>
                  ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <EmptyState title="条目不存在" hint="它可能已被删除，或项目已经切换。" />
      )}
    </Drawer>
  );
}
