import { useEffect, useId, useState, type CSSProperties } from "react";
import { AlertTriangle, FolderOpen, Info } from "lucide-react";
import { Badge, Button, Field, Input, Segmented } from "../../ui/primitives";
import { Dialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import {
  absolutePathError,
  createProject,
  desktopBridge,
  errorMessage,
  findDuplicateProject,
  findNameConflict,
  folderNameFromPath,
  importProject,
  type ProjectRecord,
} from "./projects-api";

type ProjectMode = "import" | "create";

const inlineLinkStyle: CSSProperties = {
  padding: 0,
  border: 0,
  background: "transparent",
  color: "inherit",
  fontSize: "inherit",
  textDecoration: "underline",
  cursor: "pointer",
  marginLeft: 4,
};

export function ProjectDialog({
  open,
  initialMode,
  projects,
  onClose,
  onCreated,
  onOpenExisting,
}: {
  open: boolean;
  initialMode: ProjectMode;
  projects: ProjectRecord[];
  onClose: () => void;
  onCreated: (project: ProjectRecord) => Promise<void> | void;
  onOpenExisting?: (project: ProjectRecord) => void;
}) {
  const toast = useToast();
  const pathId = useId();
  const nameId = useId();
  const hintId = useId();
  const [mode, setMode] = useState<ProjectMode>(initialMode);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const bridge = desktopBridge();
  const canPick = typeof bridge?.pickDirectory === "function";
  const trimmedPath = path.trim();
  const trimmedName = name.trim();
  const effectiveName = trimmedName || folderNameFromPath(trimmedPath);
  const duplicate = touched && trimmedPath ? findDuplicateProject(projects, trimmedPath) : null;
  const nameConflict =
    touched && effectiveName ? findNameConflict(projects, effectiveName) : null;
  const syntaxError = touched ? absolutePathError(trimmedPath) : null;

  useEffect(() => {
    if (!open) return;
    setMode(initialMode);
    setPath("");
    setName("");
    setTouched(false);
    setBusy(false);
    setPicking(false);
    setFormError(null);
  }, [open, initialMode]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(
      () => document.getElementById(pathId)?.focus(),
      60,
    );
    return () => window.clearTimeout(timer);
  }, [open, initialMode, pathId]);

  const pickFolder = async () => {
    if (!canPick || busy) return;
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

  const submit = async () => {
    setTouched(true);
    const invalidPath = absolutePathError(trimmedPath);
    if (invalidPath) {
      setFormError(invalidPath);
      return;
    }
    const existingPathProject = trimmedPath
      ? findDuplicateProject(projects, trimmedPath)
      : null;
    if (existingPathProject) {
      setFormError(
        `该目录已登记为「${existingPathProject.label || existingPathProject.name}」，请直接打开已有项目。`,
      );
      return;
    }

    if (mode === "create" && !effectiveName) {
      setFormError("请填写工作区名称，或选择包含名称的目录");
      return;
    }
    if (effectiveName.length > 200) {
      setFormError("项目名过长（上限 200 字符）");
      return;
    }
    if (nameConflict) {
      setFormError(`项目名「${effectiveName}」已被使用，请换一个名称`);
      return;
    }

    setFormError(null);
    setBusy(true);
    try {
      const project =
        mode === "import"
          ? await importProject(trimmedPath, trimmedName || undefined)
          : await createProject(effectiveName, trimmedPath);
      toast.success(
        mode === "import" ? "目录已导入" : "工作区已创建",
        project.root_path,
      );
      await onCreated(project);
    } catch (error) {
      const message = errorMessage(error);
      setFormError(message);
      toast.error(mode === "import" ? "导入目录失败" : "创建工作区失败", message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!busy) onClose();
      }}
      title={mode === "import" ? "导入已有目录" : "新建工作区"}
      description={
        mode === "import"
          ? "把一个本地目录接入工作台，不移动、不复制原文件。"
          : "为一个新目录建立受管工作区；目录不存在时由后端创建。"
      }
      width={560}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={busy}>
            {mode === "import" ? "导入并打开" : "创建并打开"}
          </Button>
        </>
      }
    >
      <form
        className="pj-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Segmented
          value={mode}
          ariaLabel="接入方式"
          options={[
            { value: "import", label: "导入目录" },
            { value: "create", label: "新建工作区" },
          ]}
          onChange={(next) => {
            setMode(next);
            setFormError(null);
            setTouched(false);
          }}
        />

        <div className="pj-dialog-note">
          <Info size={14} />
          <span>
            {mode === "import"
              ? "导入要求目录已存在且可写；工作台只登记真实路径。"
              : "新目录将由后端按绝对路径创建，并立即成为当前项目。"}
          </span>
        </div>

        <Field label="项目目录" htmlFor={pathId}>
          <div className="pj-path-row">
            <Input
              id={pathId}
              data-autofocus
              value={path}
              onChange={(event) => {
                setPath(event.target.value);
                setFormError(null);
              }}
              onBlur={() => setTouched(true)}
              placeholder="D:\work\my-project"
              spellCheck={false}
              autoComplete="off"
              aria-invalid={Boolean(syntaxError)}
              aria-describedby={hintId}
            />
            {canPick ? (
              <Button
                onClick={() => void pickFolder()}
                loading={picking}
                disabled={busy}
              >
                <FolderOpen size={14} /> 选择
              </Button>
            ) : null}
          </div>
        </Field>

        <div id={hintId} className={syntaxError ? "pj-hint is-danger" : "pj-hint"}>
          {syntaxError ??
            (duplicate ? (
              <>
                该目录已属于「{duplicate.label || duplicate.name}」。
                {onOpenExisting ? (
                  <button
                    type="button"
                    style={inlineLinkStyle}
                    onClick={() => onOpenExisting(duplicate)}
                  >
                    直接打开已有项目
                  </button>
                ) : null}
              </>
            ) : (
              "支持 Windows 盘符、UNC 与 Linux 绝对路径。"
            ))}
        </div>

        <Field
          label={mode === "import" ? "项目名（可选）" : "项目名"}
          htmlFor={nameId}
          hint={
            effectiveName
              ? `将显示为「${effectiveName}」；项目名在本地唯一。`
              : "留空时使用目录末级名称。"
          }
        >
          <Input
            id={nameId}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setFormError(null);
            }}
            onBlur={() => setTouched(true)}
            placeholder={folderNameFromPath(trimmedPath) || "例如：支付服务重构"}
            autoComplete="off"
            maxLength={200}
          />
        </Field>

        {nameConflict ? (
          <div className="pj-validation is-warning">
            <AlertTriangle size={14} />
            <span>
              已有同名项目「{nameConflict.label || nameConflict.name}」。请换一个名称，
              或直接返回列表打开现有项目。
            </span>
          </div>
        ) : null}

        {formError ? (
          <div className="pj-validation" role="alert">
            <AlertTriangle size={14} />
            <span>
              {formError}
              {duplicate && onOpenExisting ? (
                <button
                  type="button"
                  style={inlineLinkStyle}
                  onClick={() => onOpenExisting(duplicate)}
                >
                  直接打开已有项目
                </button>
              ) : null}
            </span>
          </div>
        ) : null}

        <div className="pj-dialog-footnote">
          <Badge tone="outline" dot>
            本地路径
          </Badge>
          <span>登记不会改动仓库内容；写入仍受任务隔离与写范围约束。</span>
        </div>
      </form>
    </Dialog>
  );
}
