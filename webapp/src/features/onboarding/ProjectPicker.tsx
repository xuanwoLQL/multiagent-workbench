import { useId, useState } from "react";
import {
  AlertTriangle,
  ChevronRight,
  FolderOpen,
  Info,
  Plus,
  RefreshCw,
} from "lucide-react";
import { Badge, Button, Field, Input } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import {
  createWorkspace,
  desktopBridge,
  findDuplicateProject,
  findNameConflict,
  folderNameFromPath,
  formatLastActive,
  importDirectory,
  pathSyntaxError,
  type ProjectSummary,
  type ProjectRecord,
  useProjectSummaries,
} from "./project-api";

export function ProjectPicker({
  projects,
  loading,
  error,
  onRetry,
  onOpen,
  onOpened,
}: {
  projects: ProjectRecord[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  onOpen: (project: ProjectRecord) => void;
  onOpened: (project: ProjectRecord) => void;
}) {
  const toast = useToast();
  const summaries = useProjectSummaries(projects);
  const hintId = useId();

  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState<"import" | "create" | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  const bridge = desktopBridge();
  const canPick = typeof bridge?.pickDirectory === "function";
  const trimmedPath = path.trim();
  const syntaxError = touched && trimmedPath ? pathSyntaxError(trimmedPath) : null;
  const duplicate = trimmedPath ? findDuplicateProject(projects, trimmedPath) : null;
  const effectiveName = name.trim() || folderNameFromPath(trimmedPath);
  const nameConflict = effectiveName ? findNameConflict(projects, effectiveName) : null;
  const blockingNameConflict =
    nameConflict && nameConflict.id !== duplicate?.id ? nameConflict : null;

  const pickFolder = async () => {
    if (!canPick) return;
    setPicking(true);
    try {
      const selected = await bridge?.pickDirectory?.();
      if (typeof selected === "string" && selected.trim()) {
        setPath(selected);
        setTouched(true);
        setFormError(null);
        if (!name.trim()) setName(folderNameFromPath(selected));
      }
    } catch (error) {
      toast.error("无法打开文件夹选择器", errorMessage(error));
    } finally {
      setPicking(false);
    }
  };

  const submit = async (mode: "import" | "create") => {
    setTouched(true);
    const invalid = pathSyntaxError(trimmedPath);
    if (invalid) {
      setFormError(invalid);
      return;
    }
    if (duplicate) {
      setFormError(`该目录已登记为「${duplicate.label || duplicate.name}」`);
      return;
    }
    if (blockingNameConflict) {
      setFormError(`项目名「${effectiveName}」已被使用，请换一个名称`);
      return;
    }
    setFormError(null);
    setBusy(mode);
    try {
      const project =
        mode === "import"
          ? await importDirectory(trimmedPath, name.trim() || undefined)
          : await createWorkspace(trimmedPath, name.trim() || undefined);
      toast.success(
        mode === "import" ? "目录已导入" : "工作区已创建",
        project.root_path,
      );
      setPath("");
      setName("");
      setTouched(false);
      onOpened(project);
    } catch (error) {
      const message = errorMessage(error);
      setFormError(message);
      toast.error(mode === "import" ? "导入目录失败" : "创建工作区失败", message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="ob-picker" aria-label="打开或新建项目">
      <header className="ob-picker-head">
        <div>
          <h2>选择一个工作区</h2>
          <p>打开已有项目，或把一个本地目录接入工作台。</p>
        </div>
        <Badge tone="outline">{projects.length} 个项目</Badge>
      </header>

      <ProjectList
        summaries={summaries}
        loading={loading}
        error={error}
        onRetry={onRetry}
        onOpen={onOpen}
      />

      <div className="ob-connect">
        <div className="ob-connect-head">
          <h3>接入本地目录</h3>
          <span>{canPick ? "桌面端可直接选择文件夹" : "浏览器模式：填写绝对路径"}</span>
        </div>

        <Field
          label="项目目录"
          hint="导入要求目录已存在且可写，且不会移动或复制文件；新建工作区会在目录不存在时自动创建。"
        >
          <div className="ob-path-row">
            <Input
              value={path}
              onChange={(event) => {
                setPath(event.target.value);
                setFormError(null);
              }}
              onBlur={() => setTouched(true)}
              placeholder="D:\work\my-project"
              spellCheck={false}
              className="mono"
              aria-invalid={Boolean(syntaxError)}
              aria-describedby={hintId}
            />
            {canPick ? (
              <Button onClick={() => void pickFolder()} loading={picking} disabled={Boolean(busy)}>
                <FolderOpen size={14} /> 打开文件夹
              </Button>
            ) : null}
          </div>
        </Field>

        <div id={hintId} className={syntaxError ? "ob-hint is-danger" : "ob-hint"}>
          {syntaxError ??
            (trimmedPath && !duplicate
              ? "将按绝对路径登记；路径中的符号链接会按真实位置解析。"
              : "支持 Windows 盘符、UNC 与 Linux 绝对路径。")}
        </div>

        {duplicate ? (
          <div className="ob-note is-warning">
            <AlertTriangle size={14} />
            <span>
              该目录已登记为「{duplicate.label || duplicate.name}」。
              <button
                type="button"
                className="ob-inline-link"
                onClick={() => onOpen(duplicate)}
              >
                直接打开
              </button>
            </span>
          </div>
        ) : null}

        <Field
          label="项目名称（可选）"
          hint={
            effectiveName
              ? `留空时使用「${effectiveName}」，在顶栏与任务页展示。`
              : "留空时使用目录末级名称。"
          }
        >
          <Input
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setFormError(null);
            }}
            placeholder="例如：官网重构"
          />
        </Field>

        {blockingNameConflict ? (
          <div className="ob-note is-warning">
            <AlertTriangle size={14} />
            <span>
              已有同名项目「{blockingNameConflict.label || blockingNameConflict.name}」。
              换一个名称，或直接打开已有项目。
            </span>
          </div>
        ) : null}

        <div className="ob-actions">
          <Button
            variant="primary"
            onClick={() => void submit("import")}
            loading={busy === "import"}
            disabled={Boolean(busy) || Boolean(duplicate) || Boolean(blockingNameConflict)}
          >
            <FolderOpen size={14} /> 导入目录
          </Button>
          <Button
            onClick={() => void submit("create")}
            loading={busy === "create"}
            disabled={Boolean(busy) || Boolean(duplicate) || Boolean(blockingNameConflict)}
          >
            <Plus size={14} /> 新建工作区
          </Button>
        </div>

        {error ? (
          <div className="ob-hint is-warning">
            项目列表未能加载，暂时无法判断该目录是否已经登记。
          </div>
        ) : null}

        <div className="ob-plan">
          <Info size={14} />
          <span>
            导入只登记已有目录，不移动文件；新建会在该路径创建工作区（目录不存在时自动创建）。
            两种方式都会写入 <code>.multiagent/project.json</code>，并在打开后检测 Git 与 Agent。
          </span>
        </div>

        {formError ? (
          <div className="ob-alert" role="alert">
            <AlertTriangle size={14} />
            <span>{formError}</span>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function ProjectList({
  summaries,
  loading,
  error,
  onRetry,
  onOpen,
}: {
  summaries: ProjectSummary[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  onOpen: (project: ProjectRecord) => void;
}) {
  if (loading) {
    return (
      <div className="ob-project-list" aria-busy="true">
        {[0, 1, 2].map((index) => (
          <div className="ob-project is-skeleton" key={index}>
            <span className="skeleton" style={{ width: 28, height: 28 }} />
            <span className="ob-project-main">
              <span className="skeleton" style={{ width: 120, height: 12 }} />
              <span className="skeleton" style={{ width: 220, height: 10 }} />
            </span>
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="ob-project-list">
        <div className="ob-list-state is-error" role="alert">
          <AlertTriangle size={15} />
          <div>
            <strong>无法读取项目列表</strong>
            <span>{errorMessage(error)}</span>
          </div>
          <Button size="sm" onClick={onRetry}>
            <RefreshCw size={13} /> 重试
          </Button>
        </div>
      </div>
    );
  }

  if (!summaries.length) {
    return (
      <div className="ob-project-list">
        <div className="ob-list-state">
          <FolderOpen size={16} />
          <div>
            <strong>还没有登记的项目</strong>
            <span>从下方选择或填写一个本地目录开始；第一个项目会出现在这里。</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="ob-project-list">
      {summaries.map((summary) => (
        <button
          key={summary.project.id}
          type="button"
          className="ob-project"
          onClick={() => onOpen(summary.project)}
        >
          <span className="ob-project-avatar">{initials(summary.project.label || summary.project.name)}</span>
          <span className="ob-project-main">
            <span className="ob-project-name">
              {summary.project.label || summary.project.name}
            </span>
            <span className="ob-project-path mono" title={summary.project.root_path}>
              {summary.project.root_path}
            </span>
          </span>
          <span className="ob-project-meta">
            <span className={summary.taskCount === null ? "is-loading" : undefined}>
              {summary.taskCount === null
                ? summary.dashboardLoading
                  ? "读取中…"
                  : "任务数未知"
                : `${summary.taskCount} 个任务`}
            </span>
            <span>{formatLastActive(summary)}</span>
          </span>
          <ChevronRight size={14} className="muted" />
        </button>
      ))}
    </div>
  );
}

function initials(value: string): string {
  const clean = (value || "?").trim();
  const parts = clean.split(/[\s·/_-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return clean.slice(0, 2).toUpperCase();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

