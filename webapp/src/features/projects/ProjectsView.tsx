import { useDeferredValue, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronRight,
  FolderInput,
  FolderKanban,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { qk } from "../../api/client";
import { useProjects } from "../../app/queries";
import { useUi } from "../../app/store";
import { fmtInt } from "../../lib/format";
import {
  Badge,
  Button,
  EmptyState,
  IconButton,
  Input,
  Panel,
  Segmented,
  Stat,
} from "../../ui/primitives";
import { ConfirmDialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import { ProjectCard, ProjectCardSkeleton } from "./ProjectCard";
import { ProjectDialog } from "./ProjectDialog";
import { groupDuplicateProjects } from "./duplicates";
import {
  deleteProject,
  desktopBridge,
  errorMessage,
  useProjectCards,
  type ProjectRecord,
} from "./projects-api";
import "./projects.css";

type SortKey = "activity" | "name" | "tasks";
type DialogMode = "import" | "create";

function displayName(project: ProjectRecord): string {
  return project.label || project.name || "未命名项目";
}

export function ProjectsView() {
  const client = useQueryClient();
  const toast = useToast();
  const projectsQuery = useProjects();
  const projects = (projectsQuery.data ?? []) as ProjectRecord[];
  const cards = useProjectCards(projects);

  const currentProjectId = useUi((state) => state.projectId);
  const setProject = useUi((state) => state.setProject);
  const setView = useUi((state) => state.setView);

  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [sort, setSort] = useState<SortKey>("activity");
  const [duplicatesOnly, setDuplicatesOnly] = useState(false);
  const [duplicatesOpen, setDuplicatesOpen] = useState(true);
  const [dialogMode, setDialogMode] = useState<DialogMode | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProjectRecord | null>(null);
  const [deleting, setDeleting] = useState(false);

  const duplicateGroups = useMemo(() => groupDuplicateProjects(projects), [projects]);
  const duplicateIds = useMemo(() => {
    const ids = new Set<string>();
    for (const group of duplicateGroups) {
      for (const project of group.projects) ids.add(project.id);
    }
    return ids;
  }, [duplicateGroups]);
  const duplicatesActive = duplicatesOnly && duplicateGroups.length > 0;

  const openProject = (id: string) => {
    setProject(id);
    setView("overview");
  };

  const openDirectory = async (path: string) => {
    const bridge = desktopBridge();
    if (typeof bridge?.openPath !== "function") {
      toast.info("当前为浏览器模式", "请使用桌面版在系统文件管理器中打开目录。");
      return;
    }
    try {
      const result = await bridge.openPath(path);
      if (typeof result === "string" && result.trim()) {
        toast.error("无法打开目录", result);
      }
    } catch (error) {
      toast.error("无法打开目录", errorMessage(error));
    }
  };

  const filtered = useMemo(() => {
    const needle = deferredQuery.trim().toLowerCase();
    const source = cards.filter((card) => {
      if (duplicatesActive && !duplicateIds.has(card.project.id)) return false;
      if (!needle) return true;
      const project = card.project;
      return (
        displayName(project).toLowerCase().includes(needle) ||
        String(project.name || "").toLowerCase().includes(needle) ||
        String(project.root_path || "").toLowerCase().includes(needle) ||
        String(project.id || "").toLowerCase().includes(needle)
      );
    });

    return [...source].sort((a, b) => {
      if (sort === "name") {
        return displayName(a.project).localeCompare(displayName(b.project), "zh-CN");
      }
      if (sort === "tasks") {
        const diff = (b.taskCount ?? -1) - (a.taskCount ?? -1);
        if (diff !== 0) return diff;
      }
      return (
        b.lastActiveMs - a.lastActiveMs ||
        displayName(a.project).localeCompare(displayName(b.project), "zh-CN")
      );
    });
  }, [cards, deferredQuery, sort, duplicatesActive, duplicateIds]);

  const summary = useMemo(() => {
    const loaded = cards.filter((card) => card.taskCount !== null).length;
    const totalTasks = cards.reduce((sum, card) => sum + (card.taskCount ?? 0), 0);
    const runningTasks = cards.reduce((sum, card) => sum + card.runningCount, 0);
    const runningProjects = cards.filter((card) => card.running).length;
    const failedProjects = cards.filter((card) => card.dashboardError).length;
    return {
      loaded,
      totalTasks,
      runningTasks,
      runningProjects,
      failedProjects,
      complete: loaded === cards.length,
    };
  }, [cards]);

  const handleCreated = async (project: ProjectRecord) => {
    await client.invalidateQueries({ queryKey: qk.projects });
    openProject(project.id);
  };

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      await deleteProject(deleteTarget.id);
      const remaining = projects.filter((project) => project.id !== deleteTarget.id);
      toast.success("项目已移除", displayName(deleteTarget));
      setDeleteTarget(null);
      await client.invalidateQueries({ queryKey: qk.projects });
      if (currentProjectId === deleteTarget.id) {
        setProject(remaining[0]?.id ?? null);
      }
    } catch (error) {
      toast.error("移除项目失败", errorMessage(error));
    } finally {
      setDeleting(false);
    }
  };

  const hasProjects = projects.length > 0;
  const showSkeletons = projectsQuery.isLoading;
  const isEmptySearch = !showSkeletons && hasProjects && filtered.length === 0;

  return (
    <div className="page projects-page">
      <div className="page-head">
        <div className="page-head-text">
          <div className="page-title">
            <h1>项目</h1>
            <Badge tone="outline" dot>
              {projects.length} 个工作区
            </Badge>
          </div>
          <div className="page-subtitle">
            查看所有受管工作区、Git 状态与任务负载，并在项目之间切换。
          </div>
        </div>
        <div className="page-actions">
          <Button onClick={() => setDialogMode("import")}>
            <FolderInput size={14} /> 导入目录
          </Button>
          <Button variant="primary" onClick={() => setDialogMode("create")}>
            <Plus size={14} /> 新建工作区
          </Button>
        </div>
      </div>

      {duplicateGroups.length > 0 ? (
        <section className="pj-dup-banner" aria-labelledby="pj-dup-banner-title">
          <div className="pj-dup-head">
            <button
              type="button"
              className="pj-dup-toggle"
              aria-expanded={duplicatesOpen}
              aria-controls="pj-dup-banner-body"
              onClick={() => setDuplicatesOpen((open) => !open)}
            >
              <AlertTriangle size={14} />
              <span id="pj-dup-banner-title" className="pj-dup-title">
                发现 {duplicateGroups.length} 组重复项目（同一目录）
              </span>
              <ChevronRight
                size={14}
                className={
                  duplicatesOpen ? "pj-dup-chevron is-open" : "pj-dup-chevron"
                }
              />
            </button>
            <label className="pj-dup-filter">
              <input
                type="checkbox"
                checked={duplicatesOnly}
                onChange={(event) => setDuplicatesOnly(event.target.checked)}
              />
              仅显示重复项目
            </label>
          </div>

          {duplicatesOpen ? (
            <div className="pj-dup-body" id="pj-dup-banner-body">
              <p className="pj-dup-note">
                历史数据未删除；新建/导入同一目录已被 409 拦截。建议只保留一个项目，其余请人工确认后删除。
              </p>
              <ul className="pj-dup-groups">
                {duplicateGroups.map((group) => (
                  <li key={group.key} className="pj-dup-group">
                    <div className="pj-dup-paths">
                      <div className="pj-dup-path-cell">
                        <span className="pj-dup-path-label">登记路径</span>
                        <code className="pj-dup-path" title={group.rootPath}>
                          {group.rootPath}
                        </code>
                      </div>
                      <span className="pj-dup-arrow" aria-hidden="true">
                        →
                      </span>
                      <div className="pj-dup-path-cell">
                        <span className="pj-dup-path-label">规范化后</span>
                        <code className="pj-dup-path" title={group.key}>
                          {group.key}
                        </code>
                      </div>
                    </div>
                    <div className="pj-dup-members">
                      {group.projects.map((project) => {
                        const name = displayName(project);
                        return (
                          <span key={project.id} className="pj-dup-member">
                            <span className="pj-dup-member-name" title={name}>
                              {name}
                            </span>
                            <Button size="sm" onClick={() => openProject(project.id)}>
                              打开
                            </Button>
                          </span>
                        );
                      })}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>
      ) : null}

      <div className="pj-summary" aria-label="项目汇总">
        <Stat label="工作区" value={fmtInt(projects.length)} hint="本地登记" />
        <Stat
          label="任务总数"
          value={fmtInt(summary.totalTasks)}
          hint={
            summary.complete
              ? "全部工作区"
              : `${summary.loaded}/${cards.length} 个工作区已汇总`
          }
        />
        <Stat
          label="运行中"
          value={fmtInt(summary.runningTasks)}
          tone={summary.runningTasks > 0 ? "success" : "default"}
          hint={
            summary.runningTasks > 0
              ? `${summary.runningProjects} 个工作区有活动`
              : "当前全部空闲"
          }
        />
      </div>

      <div className="pj-toolbar">
        <div className="pj-search">
          <Search size={14} className="pj-search-icon" aria-hidden="true" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索名称或路径"
            aria-label="搜索项目"
            spellCheck={false}
          />
          {query ? (
            <button
              type="button"
              className="pj-search-clear"
              aria-label="清空搜索"
              onClick={() => setQuery("")}
            >
              <X size={13} />
            </button>
          ) : null}
        </div>

        <Segmented
          value={sort}
          ariaLabel="项目排序"
          options={[
            { value: "activity", label: "最近活动" },
            { value: "tasks", label: "任务数" },
            { value: "name", label: "名称" },
          ]}
          onChange={setSort}
        />

        <span className="pj-toolbar-meta">
          {deferredQuery.trim() || duplicatesActive
            ? `${filtered.length} / ${cards.length}`
            : `${cards.length} 个工作区`}
        </span>
        <span className="pj-toolbar-spacer" />
        <IconButton
          label="刷新项目"
          title="刷新项目与统计"
          onClick={() => void projectsQuery.refetch()}
          disabled={projectsQuery.isFetching}
        >
          <RefreshCw size={14} className={projectsQuery.isFetching ? "spin" : undefined} />
        </IconButton>
      </div>

      {projectsQuery.isError ? (
        <Panel>
          <EmptyState
            icon={<AlertTriangle size={18} />}
            title="无法加载项目"
            hint={errorMessage(projectsQuery.error)}
            action={
              <Button
                onClick={() => void projectsQuery.refetch()}
                loading={projectsQuery.isFetching}
              >
                <RefreshCw size={14} /> 重试
              </Button>
            }
          />
        </Panel>
      ) : showSkeletons ? (
        <div className="pj-grid" aria-busy="true" aria-label="正在加载项目">
          {Array.from({ length: 6 }).map((_, index) => (
            <ProjectCardSkeleton key={index} />
          ))}
        </div>
      ) : !hasProjects ? (
        <Panel>
          <EmptyState
            icon={<FolderKanban size={18} />}
            title="还没有工作区"
            hint="导入一个已有目录，或为新的代码库创建受管工作区。"
            action={
              <div className="pj-empty-actions">
                <Button onClick={() => setDialogMode("import")}>
                  <FolderInput size={14} /> 导入目录
                </Button>
                <Button variant="primary" onClick={() => setDialogMode("create")}>
                  <Plus size={14} /> 新建工作区
                </Button>
              </div>
            }
          />
        </Panel>
      ) : isEmptySearch ? (
        <Panel>
          <EmptyState
            icon={<Search size={18} />}
            title={duplicatesActive ? "没有匹配的重复项目" : "没有匹配的工作区"}
            hint={
              deferredQuery.trim()
                ? `没有名称或路径包含「${deferredQuery.trim()}」的项目。`
                : "没有可显示的重复项目，请关闭筛选查看全部工作区。"
            }
            action={
              <div className="pj-empty-actions">
                {deferredQuery.trim() ? (
                  <Button onClick={() => setQuery("")}>清空搜索</Button>
                ) : null}
                {duplicatesActive ? (
                  <Button onClick={() => setDuplicatesOnly(false)}>
                    显示全部项目
                  </Button>
                ) : null}
              </div>
            }
          />
        </Panel>
      ) : (
        <div className="pj-grid">
          {filtered.map((card) => (
            <ProjectCard
              key={card.project.id}
              card={card}
              disabledDelete={projects.length <= 1}
              onOpen={() => openProject(card.project.id)}
              onOpenDirectory={() => void openDirectory(card.project.root_path)}
              onDelete={() => setDeleteTarget(card.project)}
            />
          ))}
        </div>
      )}

      {summary.failedProjects && !projectsQuery.isError ? (
        <div className="pj-partial-warning" role="status">
          <AlertTriangle size={13} />
          {summary.failedProjects} 个工作区的统计数据暂时不可用；卡片内可单独重试。
        </div>
      ) : null}

      <ProjectDialog
        open={dialogMode !== null}
        initialMode={dialogMode ?? "import"}
        projects={projects}
        onClose={() => setDialogMode(null)}
        onCreated={handleCreated}
        onOpenExisting={(project) => {
          setDialogMode(null);
          openProject(project.id);
        }}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="移除项目"
        danger
        confirmLabel={deleting ? "移除中…" : "移除项目"}
        message={
          deleteTarget ? (
            <div className="pj-confirm">
              <p>
                从工作台移除「<strong>{displayName(deleteTarget)}</strong>」？
              </p>
              <div className="pj-confirm-path">{deleteTarget.root_path}</div>
              <p className="pj-confirm-note">
                只删除工作台登记与本地记录，不会删除磁盘上的项目文件。
              </p>
            </div>
          ) : null
        }
        onCancel={() => {
          if (!deleting) setDeleteTarget(null);
        }}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}
