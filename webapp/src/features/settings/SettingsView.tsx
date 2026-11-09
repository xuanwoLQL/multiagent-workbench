import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Archive,
  Database,
  FolderInput,
  FolderOpen,
  Gauge,
  GitMerge,
  HardDrive,
  Laptop,
  Moon,
  Palette,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Sun,
  Trash2,
  Wrench,
} from "lucide-react";
import { ApiError, api, qk } from "../../api/client";
import type { Project } from "../../api/types";
import { useProjects } from "../../app/queries";
import { useUi } from "../../app/store";
import { fmtBytes, fmtDateTime, fmtInt } from "../../lib/format";
import { Badge, Button, EmptyState, Field, Input, Segmented } from "../../ui/primitives";
import { ConfirmDialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import { JsonEditor } from "./JsonEditor";
import "./settings.css";

interface ConfigFilesResponse {
  dir: string;
  files: Record<string, unknown | null>;
  secrets_redacted?: boolean;
}

interface PolicySource {
  name: string;
  content: unknown;
  fromFallback: boolean;
}

interface StorageDirectory {
  bytes?: number;
  files?: number;
}

interface StorageInfo {
  data_dir: string;
  db: {
    path?: string;
    bytes?: number;
    wal_bytes?: number;
    shm_bytes?: number;
    schema_version?: number;
  };
  dirs: Record<string, StorageDirectory>;
  counts: Record<string, number>;
  projects: Array<{
    id: string;
    name: string;
    root_path: string;
    runtime_bytes?: number;
    runtime_files?: number;
  }>;
  server_time?: string;
}

interface BackupInfo {
  path: string;
  name: string;
  bytes?: number;
  sha256?: string;
  label?: string;
  created_at?: string;
  schema_version?: number;
}

interface BackupResult {
  path?: string;
  bytes?: number;
  sha256?: string;
  manifest_path?: string;
}

interface MaintenanceReport {
  dry_run?: boolean;
  freed_bytes?: number;
  runs?: {
    pruned_workspaces?: unknown[];
    truncated?: unknown[];
    kept?: number;
  };
  tasks?: { removed_workspaces?: unknown[] };
  snapshots?: { removed?: unknown[] };
  blobs?: { removed?: unknown[]; freed_bytes?: number };
  backups?: { removed?: unknown[]; freed_bytes?: number };
  vacuum?: unknown;
}

interface SectionDefinition {
  id: string;
  label: string;
  icon: ReactNode;
}

const SETTINGS_SECTIONS: SectionDefinition[] = [
  { id: "settings-project", label: "项目", icon: <FolderOpen size={15} /> },
  { id: "settings-appearance", label: "外观", icon: <Palette size={15} /> },
  { id: "settings-policy", label: "并发与策略", icon: <Gauge size={15} /> },
  { id: "settings-agents", label: "Agent 配置", icon: <Wrench size={15} /> },
  { id: "settings-data", label: "数据", icon: <Database size={15} /> },
];

export function SettingsView() {
  const client = useQueryClient();
  const toast = useToast();
  const [activeSection, setActiveSection] = useState(SETTINGS_SECTIONS[0].id);
  const [refreshing, setRefreshing] = useState(false);

  const jumpTo = (id: string) => {
    setActiveSection(id);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const refreshAll = async () => {
    setRefreshing(true);
    try {
      await Promise.all([
        client.invalidateQueries({ queryKey: qk.projects }),
        client.invalidateQueries({ queryKey: ["config-files"] }),
        client.invalidateQueries({ queryKey: ["config-file", "policy"] }),
        client.invalidateQueries({ queryKey: ["system-storage"] }),
        client.invalidateQueries({ queryKey: ["system-backups"] }),
      ]);
      toast.success("设置已重新读取");
    } catch (error) {
      toast.error("刷新设置失败", errorMessage(error));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="page settings-page">
      <div className="page-head">
        <div className="page-head-text">
          <div className="page-title">
            <h1>设置</h1>
            <Badge tone="outline" dot>
              保存后立即生效
            </Badge>
          </div>
          <div className="page-subtitle">
            管理项目登记、界面外观、调度策略、Agent 配置与本机数据；所有改动都通过真实 API 落盘。
          </div>
        </div>
        <div className="page-actions">
          <Button onClick={() => void refreshAll()} loading={refreshing}>
            <RefreshCw size={14} /> 重新读取
          </Button>
        </div>
      </div>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分区">
          <div className="settings-nav-label">SETTINGS</div>
          {SETTINGS_SECTIONS.map((section) => (
            <button
              key={section.id}
              type="button"
              className={`settings-nav-item${activeSection === section.id ? " is-active" : ""}`}
              onClick={() => jumpTo(section.id)}
              aria-current={activeSection === section.id ? "step" : undefined}
            >
              {section.icon}
              <span>{section.label}</span>
            </button>
          ))}
          <div className="settings-nav-foot">
            <ShieldCheck size={13} />
            本地优先，配置留在本机。
          </div>
        </nav>

        <div className="settings-content">
          <ProjectSettings />
          <AppearanceSettings />
          <PolicySettings />
          <AgentConfigSettings />
          <DataSettings />
        </div>
      </div>
    </div>
  );
}

function ProjectSettings() {
  const projects = useProjects();
  const projectId = useUi((state) => state.projectId);
  const setProject = useUi((state) => state.setProject);
  const client = useQueryClient();
  const toast = useToast();
  const project = projects.data?.find((item) => item.id === projectId) ?? null;

  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [savingProject, setSavingProject] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deletingProject, setDeletingProject] = useState(false);

  const [workspacePath, setWorkspacePath] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [workspaceBusy, setWorkspaceBusy] = useState<"import" | "create" | null>(null);

  useEffect(() => {
    if (!project) return;
    setName(project.name);
    setLabel(project.label ?? "");
  }, [project?.id, project?.name, project?.label]);

  const normalizedName = name.trim();
  const normalizedLabel = label.trim();
  const projectDirty =
    Boolean(project) &&
    (normalizedName !== project?.name || normalizedLabel !== (project?.label ?? ""));

  const saveProject = async () => {
    if (!project || !normalizedName || !projectDirty) return;
    setSavingProject(true);
    try {
      const updated = await api.patch<Project>(`/api/projects/${project.id}`, {
        name: normalizedName,
        label: normalizedLabel,
      });
      client.setQueryData<Project[]>(qk.projects, (current) =>
        current?.map((item) => (item.id === updated.id ? updated : item)),
      );
      await Promise.all([
        client.invalidateQueries({ queryKey: qk.projects }),
        client.invalidateQueries({ queryKey: qk.dashboard(project.id) }),
      ]);
      setName(updated.name);
      setLabel(updated.label ?? "");
      toast.success("项目信息已更新", updated.label || updated.name);
    } catch (error) {
      toast.error("项目更新失败", errorMessage(error));
    } finally {
      setSavingProject(false);
    }
  };

  const resetProjectForm = () => {
    if (!project) return;
    setName(project.name);
    setLabel(project.label ?? "");
  };

  const addWorkspace = async (mode: "import" | "create") => {
    const rootPath = workspacePath.trim();
    if (!rootPath) {
      toast.error("请填写本地目录");
      return;
    }
    setWorkspaceBusy(mode);
    try {
      const created =
        mode === "import"
          ? await importProjectCompat(rootPath, workspaceName.trim())
          : await createProject(rootPath, workspaceName.trim());
      await client.invalidateQueries({ queryKey: qk.projects });
      setWorkspacePath("");
      setWorkspaceName("");
      toast.success(mode === "import" ? "目录已导入" : "工作区已创建", created.root_path);
    } catch (error) {
      toast.error(mode === "import" ? "导入失败" : "创建失败", errorMessage(error));
    } finally {
      setWorkspaceBusy(null);
    }
  };

  const deleteProject = async () => {
    if (!project || deletingProject) return;
    const target = project;
    const remaining = (projects.data ?? []).filter((item) => item.id !== target.id);
    setDeleteOpen(false);
    setDeletingProject(true);
    try {
      await api.del<Record<string, unknown>>(`/api/projects/${target.id}`);
      client.removeQueries({ queryKey: qk.dashboard(target.id) });
      await client.invalidateQueries({ queryKey: qk.projects });
      setProject(remaining[0]?.id ?? null);
      toast.success("项目已删除", "来源目录中的用户文件不会被删除");
    } catch (error) {
      toast.error("项目删除失败", errorMessage(error));
    } finally {
      setDeletingProject(false);
    }
  };

  if (projects.isLoading) {
    return (
      <SettingsSection id="settings-project" title="项目" subtitle="当前工作区与项目登记">
        <div className="skeleton" style={{ height: 190 }} />
      </SettingsSection>
    );
  }

  if (projects.isError) {
    return (
      <SettingsSection id="settings-project" title="项目" subtitle="当前工作区与项目登记">
        <EmptyState
          icon={<AlertTriangle size={18} />}
          title="无法加载项目"
          hint={errorMessage(projects.error)}
          action={
            <Button onClick={() => void projects.refetch()}>
              <RefreshCw size={14} /> 重试
            </Button>
          }
        />
      </SettingsSection>
    );
  }

  if (!project) {
    return (
      <SettingsSection id="settings-project" title="项目" subtitle="当前工作区与项目登记">
        <EmptyState
          icon={<FolderOpen size={18} />}
          title="当前没有选中的项目"
          hint="先从顶栏选择一个项目，或在本页导入一个本地目录。"
        />
        <WorkspaceEntry
          path={workspacePath}
          name={workspaceName}
          busy={workspaceBusy}
          onPath={setWorkspacePath}
          onName={setWorkspaceName}
          onAction={(mode) => void addWorkspace(mode)}
        />
      </SettingsSection>
    );
  }

  return (
    <SettingsSection
      id="settings-project"
      title="项目"
      subtitle="当前工作区身份、显示名与本地目录"
      actions={<Badge tone="outline">{projects.data?.length ?? 0} 个项目</Badge>}
    >
      <div className="settings-form-grid">
        <Field
          label="项目标识"
          htmlFor="settings-project-name"
          hint="全库唯一；用于创建/导入时判重，修改不会移动目录。"
        >
          <Input
            id="settings-project-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            spellCheck={false}
          />
        </Field>
        <Field
          label="显示名称"
          htmlFor="settings-project-label"
          hint="只影响工作台中的展示，留空时回退到项目标识。"
        >
          <Input
            id="settings-project-label"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="例如：官网重构"
          />
        </Field>
      </div>

      <Field label="工作区目录" hint="目录由服务端锁定；为避免误操作，此页不提供路径改写。">
        <div className="settings-path mono">{project.root_path}</div>
      </Field>

      <div className="settings-actions">
        <Button
          variant="primary"
          onClick={() => void saveProject()}
          loading={savingProject}
          disabled={!projectDirty || !normalizedName}
        >
          <Save size={14} /> 保存项目信息
        </Button>
        <Button onClick={resetProjectForm} disabled={!projectDirty || savingProject}>
          撤销修改
        </Button>
        <span className="settings-actions-hint">
          创建于 {project.created_at ? fmtDateTime(project.created_at) : "未知时间"}
        </span>
      </div>

      <SettingsSubCard
        title="导入或新建工作区"
        description="导入会登记一个已存在的本地目录；新建会在该目录下建立工作台管理的工作区。"
        icon={<FolderInput size={15} />}
      >
        <WorkspaceEntry
          path={workspacePath}
          name={workspaceName}
          busy={workspaceBusy}
          onPath={setWorkspacePath}
          onName={setWorkspaceName}
          onAction={(mode) => void addWorkspace(mode)}
        />
      </SettingsSubCard>

      <SettingsSubCard
        title="删除项目"
        description="删除工作台的协作数据、运行副本与快照；不会删除来源目录中的用户文件。"
        icon={<Trash2 size={15} />}
        tone="danger"
        actions={
          <Button
            variant="danger"
            onClick={() => setDeleteOpen(true)}
            loading={deletingProject}
          >
            <Trash2 size={14} /> 删除当前项目
          </Button>
        }
      >
        <div className="settings-danger-copy">
          当前项目：<strong>{project.label || project.name}</strong>
          <span className="mono">{project.root_path}</span>
        </div>
      </SettingsSubCard>

      <ConfirmDialog
        open={deleteOpen}
        title="删除当前项目？"
        message={
          <div className="settings-confirm">
            <p>
              将删除 <strong>{project.label || project.name}</strong> 的工作台登记、任务、运行证据、快照与
              Agent 工作副本。
            </p>
            <p>来源目录中的用户文件不会被删除，但工作台内的协作记录不可恢复。</p>
          </div>
        }
        confirmLabel="确认删除"
        danger
        onCancel={() => setDeleteOpen(false)}
        onConfirm={() => void deleteProject()}
      />
    </SettingsSection>
  );
}

function WorkspaceEntry({
  path,
  name,
  busy,
  onPath,
  onName,
  onAction,
}: {
  path: string;
  name: string;
  busy: "import" | "create" | null;
  onPath: (value: string) => void;
  onName: (value: string) => void;
  onAction: (mode: "import" | "create") => void;
}) {
  return (
    <div className="settings-workspace-entry">
      <div className="settings-form-grid">
        <Field label="本地目录" htmlFor="settings-workspace-path" hint="必须已经存在，例如 ~/myworld">
          <Input
            id="settings-workspace-path"
            value={path}
            onChange={(event) => onPath(event.target.value)}
            placeholder="D:\work\project"
            spellCheck={false}
            className="mono"
          />
        </Field>
        <Field label="名称（可选）" htmlFor="settings-workspace-name" hint="留空时使用目录末级名称。">
          <Input
            id="settings-workspace-name"
            value={name}
            onChange={(event) => onName(event.target.value)}
            placeholder="例如：我的世界"
          />
        </Field>
      </div>
      <div className="settings-actions">
        <Button
          variant="primary"
          onClick={() => onAction("import")}
          loading={busy === "import"}
          disabled={Boolean(busy)}
        >
          <FolderInput size={14} /> 导入已有目录
        </Button>
        <Button onClick={() => onAction("create")} loading={busy === "create"} disabled={Boolean(busy)}>
          <Plus size={14} /> 新建工作区
        </Button>
      </div>
    </div>
  );
}

function AppearanceSettings() {
  const theme = useUi((state) => state.theme);
  const setTheme = useUi((state) => state.setTheme);
  const options: Array<{ value: "light" | "dark" | "system"; label: ReactNode }> = [
    {
      value: "light",
      label: (
        <>
          <Sun size={13} /> 浅色
        </>
      ),
    },
    {
      value: "dark",
      label: (
        <>
          <Moon size={13} /> 深色
        </>
      ),
    },
    {
      value: "system",
      label: (
        <>
          <Laptop size={13} /> 跟随系统
        </>
      ),
    },
  ];
  const description =
    theme === "system"
      ? "随系统外观自动切换，并在系统主题变化时即时生效。"
      : theme === "dark"
        ? "始终使用深色界面，适合夜间或低亮度环境。"
        : "始终使用浅色界面，适合白天与投屏演示。";

  return (
    <SettingsSection
      id="settings-appearance"
      title="外观"
      subtitle="界面主题只保存在本机浏览器中"
      actions={<Badge tone="outline">本地偏好</Badge>}
    >
      <div className="settings-theme-row">
        <div>
          <div className="settings-field-label">主题</div>
          <div className="settings-field-hint">{description}</div>
        </div>
        <Segmented
          value={theme}
          options={options}
          onChange={setTheme}
          ariaLabel="界面主题"
        />
      </div>
    </SettingsSection>
  );
}

function PolicySettings() {
  const client = useQueryClient();
  const toast = useToast();
  const policyQuery = useQuery<PolicySource>({
    queryKey: ["config-file", "policy"],
    queryFn: fetchPolicy,
    retry: false,
  });

  const policy = isRecord(policyQuery.data?.content) ? policyQuery.data.content : {};
  const concurrency = isRecord(policy.concurrency) ? policy.concurrency : {};
  const defaultTimeout = numberOrNull(policy.default_timeout_sec);

  const savePolicy = async (value: unknown) => {
    await putPolicy(policyQuery.data?.name ?? "policy.json", value);
    await Promise.all([
      client.invalidateQueries({ queryKey: ["config-file", "policy"] }),
      client.invalidateQueries({ queryKey: ["config-files"] }),
      client.invalidateQueries({ queryKey: qk.agentDetail }),
    ]);
    toast.success("并发与策略已保存", "policy.json 已重新加载");
  };

  return (
    <SettingsSection
      id="settings-policy"
      title="并发与策略"
      subtitle="调度并发、超时、隔离、写入边界与危险操作确认"
      actions={<Badge tone="info">服务端校验</Badge>}
    >
      {policyQuery.isLoading ? (
        <div className="skeleton" style={{ height: 300 }} />
      ) : policyQuery.isError ? (
        <EmptyState
          icon={<AlertTriangle size={18} />}
          title="无法读取策略配置"
          hint={errorMessage(policyQuery.error)}
          action={
            <Button onClick={() => void policyQuery.refetch()}>
              <RefreshCw size={14} /> 重试
            </Button>
          }
        />
      ) : (
        <>
          <div className="settings-metric-grid">
            <SettingsMetric
              icon={<Gauge size={14} />}
              label="全局并发"
              value={displayNumber(concurrency.global)}
              hint="同时运行的 Agent 调用上限"
            />
            <SettingsMetric
              icon={<FolderOpen size={14} />}
              label="单项目并发"
              value={displayNumber(concurrency.per_project)}
              hint="同一项目内并行任务上限"
            />
            <SettingsMetric
              icon={<Archive size={14} />}
              label="默认超时"
              value={defaultTimeout === null ? "未设置" : `${defaultTimeout}s`}
              hint="新任务的默认执行预算"
            />
          </div>
          <MergeDeliverySettings
            policy={policy}
            fileName={policyQuery.data?.name ?? "policy.json"}
          />
          <div className="settings-merge-advanced">
            高级入口：直接编辑完整 policy.json（保存仍经过服务端校验）。
          </div>
          <JsonEditor
            key={policyQuery.data?.name ?? "policy.json"}
            fileName={policyQuery.data?.name ?? "policy.json"}
            content={policyQuery.data?.content ?? {}}
            onSave={savePolicy}
            hint={
              policyQuery.data?.fromFallback
                ? "当前后端没有独立读取接口，已从配置目录回退读取；保存仍会经过服务端校验。"
                : "保存后服务端会重新加载配置；并发、超时与隔离策略立即影响新调度。"
            }
          />
        </>
      )}
    </SettingsSection>
  );
}

function MergeDeliverySettings({
  policy,
  fileName,
}: {
  policy: Record<string, unknown>;
  fileName: string;
}) {
  const client = useQueryClient();
  const toast = useToast();
  const git = isRecord(policy.git) ? policy.git : {};

  const serverAutoMerge = boolValue(git.auto_merge, false);
  const serverRequireCleanTree = boolValue(git.require_clean_tree, true);
  const serverKeepWorktree = boolValue(git.keep_worktree, true);
  const serverRetention = retentionText(git.worktree_retention_days);

  const [autoMerge, setAutoMerge] = useState(serverAutoMerge);
  const [requireCleanTree, setRequireCleanTree] = useState(serverRequireCleanTree);
  const [keepWorktree, setKeepWorktree] = useState(serverKeepWorktree);
  const [retention, setRetention] = useState(serverRetention);
  const [saving, setSaving] = useState(false);
  const [apiError, setApiError] = useState("");

  useEffect(() => {
    setAutoMerge(serverAutoMerge);
    setRequireCleanTree(serverRequireCleanTree);
    setKeepWorktree(serverKeepWorktree);
    setRetention(serverRetention);
  }, [serverAutoMerge, serverRequireCleanTree, serverKeepWorktree, serverRetention]);

  const retentionForCompare =
    retention.trim() === ""
      ? String(DEFAULT_WORKTREE_RETENTION_DAYS)
      : retention.trim();
  const dirty =
    autoMerge !== serverAutoMerge ||
    requireCleanTree !== serverRequireCleanTree ||
    keepWorktree !== serverKeepWorktree ||
    retentionForCompare !== serverRetention;

  const save = async () => {
    if (saving) return;
    const parsedRetention =
      retention.trim() === ""
        ? DEFAULT_WORKTREE_RETENTION_DAYS
        : Number(retention.trim());
    if (
      !Number.isInteger(parsedRetention) ||
      parsedRetention < 1 ||
      parsedRetention > 365
    ) {
      const message = "worktree 保留天数需为 1–365 的整数；留空表示默认 14 天。";
      setApiError(message);
      toast.error("交付策略保存失败", message);
      return;
    }

    setSaving(true);
    setApiError("");
    try {
      const next: Record<string, unknown> = {
        ...policy,
        git: {
          ...git,
          auto_merge: autoMerge,
          require_clean_tree: requireCleanTree,
          keep_worktree: keepWorktree,
          worktree_retention_days: parsedRetention,
        },
      };
      await putPolicy(fileName, next);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["config-file", "policy"] }),
        client.invalidateQueries({ queryKey: ["config-files"] }),
        client.invalidateQueries({ queryKey: qk.agentDetail }),
      ]);
      toast.success("交付策略已保存", "policy.json 已重新加载，新任务将按新策略交付");
    } catch (error) {
      const detail = errorMessage(error);
      setApiError(detail);
      toast.error("交付策略保存失败", detail);
    } finally {
      setSaving(false);
    }
  };

  return (
    <SettingsSubCard
      title="交付与合并"
      description="验收通过后的合并、基线检查与 worktree 回收；保存只改 git 字段，其它策略保持不变。"
      icon={<GitMerge size={15} />}
    >
      <div className="settings-merge-mode">
        <div className="settings-merge-mode-head">
          <span className="settings-merge-mode-title">当前模式</span>
          <Badge tone={autoMerge ? "warning" : "outline"} dot>
            {autoMerge ? "自动合并" : "先审后合（默认）"}
          </Badge>
        </div>
        <p className="settings-merge-mode-copy">
          {autoMerge
            ? "验收通过即 git merge --no-ff；适合受控的 DAG 连续交付，建议基线干净且测试可信。"
            : "验收通过只提交任务分支，不合并到基线；下游依赖任务需先人工合并才能看到产出。"}
        </p>
        <p className="settings-merge-mode-default">
          产品默认：先审后合（auto_merge=false）。DAG 连续交付可显式开启 auto_merge。
        </p>
      </div>

      <div className="settings-merge-list">
        <div className="settings-merge-row">
          <div className="settings-merge-copy">
            <div className="settings-merge-label">
              自动合并 <span className="mono">auto_merge</span>
            </div>
            <div className="settings-merge-hint">
              {autoMerge
                ? "验收通过即 git merge --no-ff 到基线；建议只在测试可信、基线干净时开启。"
                : "任务分支保留，需在任务详情/审阅页手动合并。"}
            </div>
          </div>
          <div className="settings-merge-control">
            <Segmented<"on" | "off">
              value={autoMerge ? "on" : "off"}
              options={[
                { value: "on", label: "开启" },
                { value: "off", label: "关闭" },
              ]}
              onChange={(value) => {
                setAutoMerge(value === "on");
                setApiError("");
              }}
              ariaLabel="自动合并 auto_merge"
            />
          </div>
        </div>

        <div className="settings-merge-row">
          <div className="settings-merge-copy">
            <div className="settings-merge-label">
              合并前要求干净基线 <span className="mono">require_clean_tree</span>
            </div>
            <div className="settings-merge-hint">
              基线有未提交改动时阻止合并，避免把人工改动混入 Agent 提交。
            </div>
          </div>
          <div className="settings-merge-control">
            <Segmented<"on" | "off">
              value={requireCleanTree ? "on" : "off"}
              options={[
                { value: "on", label: "开启" },
                { value: "off", label: "关闭" },
              ]}
              onChange={(value) => {
                setRequireCleanTree(value === "on");
                setApiError("");
              }}
              ariaLabel="合并前要求干净基线 require_clean_tree"
            />
          </div>
        </div>

        <div className="settings-merge-row">
          <div className="settings-merge-copy">
            <div className="settings-merge-label">
              合并后保留 worktree <span className="mono">keep_worktree</span>
            </div>
            <div className="settings-merge-hint">
              关闭后合并成功即回收 worktree 目录；任务分支与提交始终保留。
            </div>
          </div>
          <div className="settings-merge-control">
            <Segmented<"on" | "off">
              value={keepWorktree ? "on" : "off"}
              options={[
                { value: "on", label: "开启" },
                { value: "off", label: "关闭" },
              ]}
              onChange={(value) => {
                setKeepWorktree(value === "on");
                setApiError("");
              }}
              ariaLabel="合并后保留 worktree keep_worktree"
            />
          </div>
        </div>

        <div className="settings-merge-row">
          <div className="settings-merge-copy">
            <label className="settings-merge-label" htmlFor="settings-merge-retention">
              worktree 保留天数 <span className="mono">worktree_retention_days</span>
            </label>
            <div className="settings-merge-hint" id="settings-merge-retention-hint">
              1–365 的整数，留空表示默认 14 天；仅供 ops.prune 清理已合并 worktree 时使用。
            </div>
          </div>
          <div className="settings-merge-control">
            <Input
              id="settings-merge-retention"
              className="settings-merge-days"
              type="number"
              min={1}
              max={365}
              step={1}
              inputMode="numeric"
              value={retention}
              placeholder={String(DEFAULT_WORKTREE_RETENTION_DAYS)}
              disabled={saving}
              aria-describedby="settings-merge-retention-hint"
              onChange={(event) => {
                setRetention(event.target.value);
                setApiError("");
              }}
            />
          </div>
        </div>
      </div>

      {apiError ? (
        <div className="settings-merge-error" role="alert">
          <AlertTriangle size={13} />
          <span>{apiError}</span>
        </div>
      ) : null}

      <div className="settings-actions">
        <Button
          variant="primary"
          onClick={() => void save()}
          loading={saving}
          disabled={!dirty || saving}
        >
          <Save size={14} /> 保存交付策略
        </Button>
        <span className="settings-actions-hint">
          只写入 policy.json 的 git 字段，并发等其它策略保持不变。
        </span>
      </div>
    </SettingsSubCard>
  );
}

function AgentConfigSettings() {
  const client = useQueryClient();
  const toast = useToast();
  const configQuery = useQuery<ConfigFilesResponse>({
    queryKey: ["config-files"],
    queryFn: () => api.get<ConfigFilesResponse>("/api/config/files"),
    staleTime: 5_000,
    retry: false,
  });
  const [selectedFile, setSelectedFile] = useState("agents.json");

  const files = configQuery.data?.files ?? {};
  const fileNames = useMemo(() => orderConfigFiles(Object.keys(files)), [files]);

  useEffect(() => {
    if (!fileNames.length) return;
    if (!fileNames.includes(selectedFile)) setSelectedFile(fileNames[0]);
  }, [fileNames, selectedFile]);

  const selectedContent = files[selectedFile] ?? {};

  const saveConfigFile = async (fileName: string, value: unknown) => {
    await api.put(`/api/config/files/${encodeURIComponent(fileName)}`, { content: value });
    await Promise.all([
      client.invalidateQueries({ queryKey: ["config-files"] }),
      client.invalidateQueries({ queryKey: ["config-file", "policy"] }),
      client.invalidateQueries({ queryKey: qk.agentDetail }),
    ]);
    toast.success(`${fileName} 已保存`, "配置已重新加载并立即生效");
  };

  return (
    <SettingsSection
      id="settings-agents"
      title="Agent 配置"
      subtitle="直接编辑服务端配置文件；保存前做客户端 JSON 校验"
      actions={
        configQuery.data?.dir ? (
          <span className="settings-dir mono" title={configQuery.data.dir}>
            {configQuery.data.dir}
          </span>
        ) : null
      }
    >
      {configQuery.isLoading ? (
        <div className="skeleton" style={{ height: 420 }} />
      ) : configQuery.isError ? (
        <EmptyState
          icon={<AlertTriangle size={18} />}
          title="无法读取配置文件"
          hint={errorMessage(configQuery.error)}
          action={
            <Button onClick={() => void configQuery.refetch()}>
              <RefreshCw size={14} /> 重试
            </Button>
          }
        />
      ) : fileNames.length === 0 ? (
        <EmptyState
          icon={<Wrench size={18} />}
          title="配置目录为空"
          hint="服务端尚未返回任何允许编辑的配置文件。"
        />
      ) : (
        <>
          {configQuery.data?.secrets_redacted ? (
            <div className="settings-info">
              <ShieldCheck size={13} />
              敏感值在读取时已遮蔽；保存时服务端会把遮蔽值还原为原值，不会覆盖真实凭据。
            </div>
          ) : null}
          <div className="settings-config-layout">
            <div className="settings-file-list" role="group" aria-label="配置文件">
              {fileNames.map((fileName) => {
                const content = files[fileName];
                return (
                  <button
                    type="button"
                    key={fileName}
                    className={`settings-file-item${selectedFile === fileName ? " is-active" : ""}`}
                    onClick={() => setSelectedFile(fileName)}
                    aria-pressed={selectedFile === fileName}
                  >
                    <span className="settings-file-name mono">{fileName}</span>
                    <span className="settings-file-meta">
                      {content === null ? (
                        <Badge tone="warning">缺失</Badge>
                      ) : (
                        <span>{fmtInt(jsonSize(content))} 字符</span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
            <div className="settings-config-editor">
              <JsonEditor
                key={`${selectedFile}:${configQuery.dataUpdatedAt}`}
                fileName={selectedFile}
                content={selectedContent}
                onSave={(value) => saveConfigFile(selectedFile, value)}
                hint="保存会经过服务端配置校验；agents.json、routing.json、policy.json 等修改会立即重新加载。"
              />
            </div>
          </div>
        </>
      )}
    </SettingsSection>
  );
}

function DataSettings() {
  const client = useQueryClient();
  const toast = useToast();
  const storageQuery = useQuery<StorageInfo>({
    queryKey: ["system-storage"],
    queryFn: () => api.get<StorageInfo>("/api/system/storage"),
    retry: false,
  });
  const backupsQuery = useQuery<BackupInfo[]>({
    queryKey: ["system-backups"],
    queryFn: async () => {
      try {
        return await api.get<BackupInfo[]>("/api/system/backups");
      } catch (error) {
        if (error instanceof ApiError && [404, 405].includes(error.status)) return [];
        throw error;
      }
    },
    retry: false,
  });

  const [backupLabel, setBackupLabel] = useState("manual");
  const [backupBusy, setBackupBusy] = useState(false);
  const [maintenanceBusy, setMaintenanceBusy] = useState<"preview" | "apply" | null>(null);
  const [maintenancePreview, setMaintenancePreview] = useState<MaintenanceReport | null>(null);
  const [maintenanceApplied, setMaintenanceApplied] = useState<MaintenanceReport | null>(null);
  const [maintenanceConfirm, setMaintenanceConfirm] = useState(false);

  const invalidateSystem = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["system-storage"] }),
      client.invalidateQueries({ queryKey: ["system-backups"] }),
    ]);
  };

  const createBackup = async () => {
    setBackupBusy(true);
    try {
      const result = await api.post<BackupResult>("/api/system/backup", {
        label: backupLabel.trim() || "manual",
      });
      await invalidateSystem();
      toast.success("数据库备份已创建", result.path || "备份文件已写入本机");
    } catch (error) {
      toast.error("创建备份失败", errorMessage(error));
    } finally {
      setBackupBusy(false);
    }
  };

  const previewMaintenance = async () => {
    setMaintenanceBusy("preview");
    try {
      const result = await api.post<MaintenanceReport>("/api/system/maintenance", { apply: false });
      setMaintenancePreview(result);
      setMaintenanceApplied(null);
      toast.info("清理预览已生成", "只统计，不删除任何文件");
    } catch (error) {
      toast.error("清理预览失败", errorMessage(error));
    } finally {
      setMaintenanceBusy(null);
    }
  };

  const applyMaintenance = async () => {
    setMaintenanceConfirm(false);
    setMaintenanceBusy("apply");
    try {
      const result = await api.post<MaintenanceReport>("/api/system/maintenance", {
        apply: true,
        confirm: "APPLY",
      });
      setMaintenanceApplied(result);
      setMaintenancePreview(result);
      await invalidateSystem();
      toast.success("保留清理已执行", `释放 ${fmtBytes(result.freed_bytes)}`);
    } catch (error) {
      toast.error("执行清理失败", errorMessage(error));
    } finally {
      setMaintenanceBusy(null);
    }
  };

  const storage = storageQuery.data;
  const db = storage?.db ?? {};
  const dirs = storage?.dirs ?? {};
  const counts = storage?.counts ?? {};
  const report = maintenanceApplied ?? maintenancePreview;

  return (
    <SettingsSection
      id="settings-data"
      title="数据"
      subtitle="本机数据库、运行证据、备份与保留策略"
      actions={
        <Button size="sm" variant="ghost" onClick={() => void storageQuery.refetch()} loading={storageQuery.isFetching}>
          <RefreshCw size={13} /> 刷新
        </Button>
      }
    >
      {storageQuery.isLoading ? (
        <div className="skeleton" style={{ height: 220 }} />
      ) : storageQuery.isError ? (
        <EmptyState
          icon={<AlertTriangle size={18} />}
          title="无法读取存储信息"
          hint={errorMessage(storageQuery.error)}
          action={
            <Button onClick={() => void storageQuery.refetch()}>
              <RefreshCw size={14} /> 重试
            </Button>
          }
        />
      ) : storage ? (
        <>
          <div className="settings-path-line">
            <HardDrive size={14} />
            <span className="settings-field-label">数据目录</span>
            <span className="mono">{storage.data_dir || "—"}</span>
          </div>
          <div className="settings-storage-grid">
            <StorageCard
              icon={<Database size={15} />}
              label="SQLite"
              value={fmtBytes(db.bytes)}
              hint={`${db.schema_version ? `schema v${db.schema_version}` : "schema —"} · WAL ${fmtBytes(db.wal_bytes)}`}
            />
            <StorageCard
              icon={<Archive size={15} />}
              label="运行证据"
              value={fmtBytes(dirs.runtime?.bytes)}
              hint={`${fmtInt(dirs.runtime?.files)} 个文件`}
            />
            <StorageCard
              icon={<HardDrive size={15} />}
              label="Blobs"
              value={fmtBytes(dirs.blobs?.bytes)}
              hint={`${fmtInt(dirs.blobs?.files)} 个文件`}
            />
            <StorageCard
              icon={<FolderOpen size={15} />}
              label="快照"
              value={fmtBytes(dirs.snapshots?.bytes)}
              hint={`${fmtInt(dirs.snapshots?.files)} 个文件`}
            />
            <StorageCard
              icon={<Wrench size={15} />}
              label="日志"
              value={fmtBytes(dirs.logs?.bytes)}
              hint={`${fmtInt(dirs.logs?.files)} 个文件`}
            />
            <StorageCard
              icon={<Archive size={15} />}
              label="备份"
              value={fmtBytes(dirs.backups?.bytes)}
              hint={`${fmtInt(dirs.backups?.files)} 个文件`}
            />
          </div>
          <div className="settings-count-row">
            {["projects", "tasks", "runs", "events", "artifacts", "approvals"].map((key) => (
              <span className="settings-count-chip" key={key}>
                <span>{key}</span>
                <strong>{fmtInt(counts[key])}</strong>
              </span>
            ))}
          </div>
        </>
      ) : null}

      <div className="settings-data-columns">
        <SettingsSubCard
          title="创建备份"
          description="生成一份可恢复的一致性 SQLite 快照。"
          icon={<Archive size={15} />}
        >
          <div className="settings-inline-form">
            <Field label="备份标签" htmlFor="settings-backup-label">
              <Input
                id="settings-backup-label"
                value={backupLabel}
                onChange={(event) => setBackupLabel(event.target.value)}
                placeholder="manual"
              />
            </Field>
            <Button variant="primary" onClick={() => void createBackup()} loading={backupBusy}>
              <Archive size={14} /> 立即备份
            </Button>
          </div>
          <div className="settings-backup-list">
            {backupsQuery.isLoading ? (
              <div className="settings-empty-inline">正在读取备份记录…</div>
            ) : backupsQuery.isError ? (
              <div className="settings-empty-inline">备份列表暂不可用</div>
            ) : backupsQuery.data?.length ? (
              backupsQuery.data.slice(0, 4).map((backup) => (
                <div className="settings-backup-row" key={backup.path}>
                  <span className="settings-backup-name">{backup.label || backup.name}</span>
                  <span className="settings-backup-meta">
                    {backup.created_at ? fmtDateTime(backup.created_at) : "—"} · {fmtBytes(backup.bytes)}
                  </span>
                </div>
              ))
            ) : (
              <div className="settings-empty-inline">还没有备份。创建后会显示在这里。</div>
            )}
          </div>
        </SettingsSubCard>

        <SettingsSubCard
          title="保留清理"
          description="先预览，确认后再删除旧证据、快照与未引用 blob。"
          icon={<Wrench size={15} />}
          actions={
            <Badge tone="warning" dot>
              不可撤销
            </Badge>
          }
        >
          <div className="settings-maintenance-actions">
            <Button onClick={() => void previewMaintenance()} loading={maintenanceBusy === "preview"}>
              预览清理
            </Button>
            <Button
              variant="danger"
              onClick={() => setMaintenanceConfirm(true)}
              loading={maintenanceBusy === "apply"}
            >
              执行清理
            </Button>
          </div>
          <div className="settings-muted-copy">
            清理会按服务端默认保留策略执行，并保留数据库行与审计事件。
          </div>
          {report ? <MaintenanceReportView report={report} /> : null}
        </SettingsSubCard>
      </div>

      <ConfirmDialog
        open={maintenanceConfirm}
        title="执行保留清理？"
        message={
          <div className="settings-confirm">
            <p>将删除旧的运行证据、快照、未引用 blob 与超量备份，并执行数据库整理。</p>
            <p>此操作不可撤销。建议先执行「预览清理」核对释放空间与影响范围。</p>
          </div>
        }
        confirmLabel="确认执行"
        danger
        onCancel={() => setMaintenanceConfirm(false)}
        onConfirm={() => void applyMaintenance()}
      />
    </SettingsSection>
  );
}

function SettingsSection({
  id,
  title,
  subtitle,
  actions,
  children,
}: {
  id: string;
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section id={id} className="settings-section" aria-labelledby={`${id}-title`}>
      <header className="settings-section-head">
        <div className="settings-section-heading">
          <h2 id={`${id}-title`}>{title}</h2>
          {subtitle ? <p>{subtitle}</p> : null}
        </div>
        {actions ? <div className="settings-section-actions">{actions}</div> : null}
      </header>
      <div className="settings-section-body">{children}</div>
    </section>
  );
}

function SettingsSubCard({
  title,
  description,
  icon,
  actions,
  children,
  tone,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  tone?: "danger";
}) {
  return (
    <div className={`settings-subcard${tone ? ` is-${tone}` : ""}`}>
      <div className="settings-subcard-head">
        <div className="settings-subcard-title">
          {icon ? <span className="settings-subcard-icon">{icon}</span> : null}
          <div>
            <h3>{title}</h3>
            {description ? <p>{description}</p> : null}
          </div>
        </div>
        {actions ? <div className="settings-subcard-actions">{actions}</div> : null}
      </div>
      <div className="settings-subcard-body">{children}</div>
    </div>
  );
}

function SettingsMetric({
  icon,
  label,
  value,
  hint,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="settings-metric">
      <div className="settings-metric-label">
        {icon}
        {label}
      </div>
      <div className="settings-metric-value">{value}</div>
      <div className="settings-metric-hint">{hint}</div>
    </div>
  );
}

function StorageCard({
  icon,
  label,
  value,
  hint,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="storage-card">
      <div className="storage-card-label">
        {icon}
        {label}
      </div>
      <div className="storage-card-value">{value}</div>
      <div className="storage-card-hint">{hint}</div>
    </div>
  );
}

function MaintenanceReportView({ report }: { report: MaintenanceReport }) {
  const items = [
    { label: "释放空间", value: fmtBytes(report.freed_bytes) },
    { label: "任务工作副本", value: fmtInt(arrayLength(report.tasks?.removed_workspaces)) },
    { label: "Run 工作副本", value: fmtInt(arrayLength(report.runs?.pruned_workspaces)) },
    { label: "大输出裁剪", value: fmtInt(arrayLength(report.runs?.truncated)) },
    { label: "快照", value: fmtInt(arrayLength(report.snapshots?.removed)) },
    { label: "Blob", value: fmtInt(arrayLength(report.blobs?.removed)) },
    { label: "备份", value: fmtInt(arrayLength(report.backups?.removed)) },
  ];
  return (
    <div className="settings-maintenance-report">
      <div className="settings-maintenance-head">
        <Badge tone={report.dry_run ? "info" : "warning"} dot>
          {report.dry_run ? "仅预览，未删除" : "已执行"}
        </Badge>
      </div>
      <div className="settings-maintenance-grid">
        {items.map((item) => (
          <div className="settings-maintenance-item" key={item.label}>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </div>
        ))}
      </div>
      <details className="settings-json-details">
        <summary>查看原始报告</summary>
        <pre>{JSON.stringify(report, null, 2)}</pre>
      </details>
    </div>
  );
}

async function importProjectCompat(rootPath: string, name: string): Promise<Project> {
  try {
    return await api.post<Project>("/api/projects/import", {
      root_path: rootPath,
      name: name || undefined,
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 400 && /path/i.test(error.message)) {
      return api.post<Project>("/api/projects/import", {
        path: rootPath,
        name: name || undefined,
      });
    }
    throw error;
  }
}

async function createProject(rootPath: string, name: string): Promise<Project> {
  const fallbackName = rootPath.split(/[\\/]/).filter(Boolean).pop() || "workspace";
  const projectName = name || fallbackName;
  return api.post<Project>("/api/projects", {
    name: projectName,
    label: name || projectName,
    root_path: rootPath,
  });
}

async function fetchPolicy(): Promise<PolicySource> {
  try {
    const response = await api.get<unknown>("/api/config/files/policy");
    if (isRecord(response) && isRecord(response.files)) {
      return {
        name: "policy.json",
        content: response.files["policy.json"] ?? response.files.policy ?? {},
        fromFallback: false,
      };
    }
    if (isRecord(response) && "content" in response) {
      return {
        name: typeof response.name === "string" ? response.name : "policy",
        content: response.content,
        fromFallback: false,
      };
    }
    return { name: "policy", content: response, fromFallback: false };
  } catch (error) {
    if (!(error instanceof ApiError) || ![404, 405].includes(error.status)) throw error;
  }

  const all = await api.get<ConfigFilesResponse>("/api/config/files");
  return {
    name: "policy.json",
    content: all.files["policy.json"] ?? all.files.policy ?? {},
    fromFallback: true,
  };
}

async function putPolicy(name: string, content: unknown): Promise<void> {
  await api.put(`/api/config/files/${encodeURIComponent(name)}`, { content });
}

const DEFAULT_WORKTREE_RETENTION_DAYS = 14;

function boolValue(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["false", "0", "no", "off", "disabled"].includes(normalized)) return false;
    if (["true", "1", "yes", "on", "enabled"].includes(normalized)) return true;
  }
  return fallback;
}

function retentionText(value: unknown): string {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim() !== ""
        ? Number(value)
        : Number.NaN;
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 365) {
    return String(DEFAULT_WORKTREE_RETENTION_DAYS);
  }
  return String(parsed);
}

function orderConfigFiles(names: string[]): string[] {
  const preferred = ["agents.json", "templates.json", "routing.json", "acceptance.json", "policy.json"];
  const present = new Set(names);
  return [
    ...preferred.filter((name) => present.has(name)),
    ...names.filter((name) => !preferred.includes(name)).sort((a, b) => a.localeCompare(b)),
  ];
}

function jsonSize(value: unknown): number {
  try {
    return JSON.stringify(value).length;
  } catch {
    return 0;
  }
}

function displayNumber(value: unknown): string {
  const parsed = numberOrNull(value);
  return parsed === null ? "未设置" : fmtInt(parsed);
}

function numberOrNull(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function arrayLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
