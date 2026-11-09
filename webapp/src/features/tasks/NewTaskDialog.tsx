import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Play, Plus, Sparkles, Wand2 } from "lucide-react";
import { api, qk } from "../../api/client";
import type { AgentUsage, Task } from "../../api/types";
import { useAgents, useDashboard, useInvalidateProject } from "../../app/queries";
import { useUi } from "../../app/store";
import { Button, Field, Input, Select, Textarea } from "../../ui/primitives";
import { Dialog } from "../../ui/overlay";
import { useToast } from "../../ui/toast";
import clsx from "clsx";
import "./tasks.css";

const TEMPLATES = [
  { label: "实现一个功能", text: "实现：<功能描述>。先读现有代码与约定，完成实现并补上能验证它的测试。" },
  { label: "修复一个问题", text: "复现并修复：<问题描述>。定位根因，补回归测试，说明改动影响。" },
  { label: "重构一个模块", text: "重构 <模块>：保持行为不变，拆分职责，补齐测试，并给出迁移说明。" },
  { label: "补齐测试", text: "为 <模块> 补齐测试：覆盖主要路径、边界与错误处理，跑通项目的测试命令。" },
  { label: "写一份方案", text: "写一份技术方案：围绕 <主题>，包含背景、方案对比、风险与实施步骤，输出 docs/方案.md。" },
];

const TIMEOUT_MIN_SEC = 30;
const TIMEOUT_MAX_SEC = 86400;
const MAX_ATTEMPTS_MIN = 1;
const MAX_ATTEMPTS_MAX = 10;

/** 留空 = 使用任务类型默认；非空时必须是范围内的整数。 */
function parseOptionalInt(
  raw: string,
  label: string,
  min: number,
  max: number,
): { value: number | null; error: string | null } {
  const trimmed = raw.trim();
  if (!trimmed) return { value: null, error: null };
  if (!/^-?\d+$/.test(trimmed)) {
    return { value: null, error: `${label}需为整数` };
  }
  const value = Number(trimmed);
  if (value < min || value > max) {
    return { value: null, error: `${label}需在 ${min}–${max} 之间` };
  }
  return { value, error: null };
}

interface QuickTaskResponse {
  tasks: Task[];
  started: boolean;
  warnings?: string[];
  message?: string;
}

export function NewTaskDialog() {
  const open = useUi((state) => state.newTaskOpen);
  const setOpen = useUi((state) => state.setNewTaskOpen);
  const projectId = useUi((state) => state.projectId);
  const openTask = useUi((state) => state.openTask);
  const dashboard = useDashboard(projectId);
  const agents = useAgents();
  const invalidate = useInvalidateProject(projectId);
  const toast = useToast();
  const client = useQueryClient();

  const [text, setText] = useState("");
  const [outputs, setOutputs] = useState("");
  const [agent, setAgent] = useState("");
  const [kind, setKind] = useState("auto");
  const [isolation, setIsolation] = useState("worktree");
  const [scope, setScope] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [timeoutSec, setTimeoutSec] = useState("");
  const [maxAttempts, setMaxAttempts] = useState("");
  const [timeoutError, setTimeoutError] = useState<string | null>(null);
  const [maxAttemptsError, setMaxAttemptsError] = useState<string | null>(null);
  const [mode, setMode] = useState<"single" | "plan">("single");
  const [busy, setBusy] = useState<"create" | "start" | "plan" | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  useEffect(() => {
    if (!open) return;
    setWarnings([]);
    setTimeoutError(null);
    setMaxAttemptsError(null);
  }, [open]);

  const agentOptions = useMemo(
    () => (agents.data ?? []).filter((item) => item.enabled !== false),
    [agents.data],
  );

  const reset = () => {
    setText("");
    setOutputs("");
    setScope("");
    setTimeoutSec("");
    setMaxAttempts("");
    setTimeoutError(null);
    setMaxAttemptsError(null);
    setWarnings([]);
    setBusy(null);
  };

  const submit = async (start: boolean, plan = false) => {
    if (!projectId) return;
    if (!text.trim()) {
      toast.error("先描述要完成的任务");
      return;
    }
    const timeoutResult = parseOptionalInt(
      timeoutSec,
      "超时预算",
      TIMEOUT_MIN_SEC,
      TIMEOUT_MAX_SEC,
    );
    const attemptsResult = parseOptionalInt(
      maxAttempts,
      "最大尝试次数",
      MAX_ATTEMPTS_MIN,
      MAX_ATTEMPTS_MAX,
    );
    setTimeoutError(timeoutResult.error);
    setMaxAttemptsError(attemptsResult.error);
    if (timeoutResult.error || attemptsResult.error) {
      setAdvanced(true);
      toast.error("高级选项不合法", "请修正标红的字段后再提交");
      return;
    }
    setBusy(plan ? "plan" : start ? "start" : "create");
    try {
      const body: Record<string, unknown> = {
        text: text.trim(),
        mode: plan ? "plan" : mode,
        start: start && !plan,
        kind,
        isolation: isolation || undefined,
        agent_id: agent || undefined,
        outputs: outputs
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
        write_scope: scope
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      };
      // 只有用户实际填写且校验通过时才发送，留空保持后端的类型默认值。
      if (timeoutResult.value !== null) body.timeout_sec = timeoutResult.value;
      if (attemptsResult.value !== null) body.max_attempts = attemptsResult.value;
      const result = await api.post<QuickTaskResponse>(`/api/projects/${projectId}/quick-task`, body);
      setWarnings(result.warnings ?? []);
      invalidate();
      void client.invalidateQueries({ queryKey: qk.dashboard(projectId) });
      if (result.tasks?.length) {
        const first = result.tasks[0];
        toast.success(
          plan ? `已创建 ${result.tasks.length} 步计划` : "任务已创建",
          start ? "调度已启动" : "尚未运行",
        );
        setOpen(false);
        reset();
        if (first?.id) openTask(first.id);
      } else {
        toast.error(result.message || "任务未创建");
      }
    } catch (error) {
      toast.error("创建失败", (error as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const project = dashboard.data?.project;

  return (
    <Dialog
      open={open}
      onClose={() => setOpen(false)}
      title="新建任务"
      description={project ? `工作区：${project.label || project.name}` : undefined}
      width={640}
      footer={
        <div className="newtask-foot">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={mode === "plan"}
              onChange={(event) => setMode(event.target.checked ? "plan" : "single")}
            />
            拆成「调研 → 初稿 → 审阅」三步
          </label>
          <div className="row" style={{ gap: 8 }}>
            <Button onClick={() => void submit(false)} loading={busy === "create"} disabled={Boolean(busy)}>
              <Plus size={14} /> 仅创建
            </Button>
            <Button
              variant="primary"
              onClick={() => void submit(true)}
              loading={busy === "start" || busy === "plan"}
              disabled={Boolean(busy)}
            >
              <Play size={14} /> 创建并运行
            </Button>
          </div>
        </div>
      }
    >
      <div className="newtask">
        <textarea
          className="newtask-input"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="描述目标、约束和验收标准。写得越具体，Agent 越少走弯路。"
          rows={5}
          autoFocus
          data-autofocus
          onKeyDown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
              event.preventDefault();
              void submit(true);
            }
          }}
        />
        <div className="template-row" style={{ margin: "8px 0 2px", justifyContent: "flex-start" }}>
          {TEMPLATES.map((template) => (
            <button
              type="button"
              key={template.label}
              className="template-chip"
              onClick={() => setText(template.text)}
            >
              {template.label}
            </button>
          ))}
        </div>

        <div className="newtask-grid">
          <Field label="产出文件" hint="逗号分隔；填写后会按「文件存在」验收">
            <Input
              value={outputs}
              onChange={(event) => setOutputs(event.target.value)}
              placeholder="src/feature.ts, docs/design.md"
              spellCheck={false}
            />
          </Field>
          <Field label="执行 Agent">
            <Select value={agent} onChange={(event) => setAgent(event.target.value)}>
              <option value="">自动选择</option>
              {agentOptions.map((item) => {
                const key = item.agent_id || item.id || "agent";
                return (
                <option key={key} value={key}>
                  {item.label || key}
                </option>
                );
              })}
            </Select>
          </Field>
          <Field label="任务类型">
            <Select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="auto">自动判断</option>
              <option value="code">代码</option>
              <option value="doc">文档</option>
              <option value="research">调研</option>
              <option value="review">审阅</option>
              <option value="data">数据</option>
              <option value="web">Web</option>
              <option value="generic">通用</option>
            </Select>
          </Field>
          <Field label="写入方式">
            <Select value={isolation} onChange={(event) => setIsolation(event.target.value)}>
              <option value="worktree">Git worktree（推荐）</option>
              <option value="copy">隔离副本</option>
              <option value="shared">直接写入工作区</option>
            </Select>
          </Field>
        </div>

        <button
          type="button"
          className={clsx("disclosure", advanced && "is-open")}
          aria-expanded={advanced}
          aria-controls="newtask-advanced-options"
          onClick={() => setAdvanced((value) => !value)}
        >
          <ChevronDown size={14} aria-hidden="true" /> 高级选项
        </button>
        {advanced ? (
          <div className="newtask-advanced" id="newtask-advanced-options">
            <div className="newtask-grid" style={{ marginTop: 10 }}>
              <Field label="写范围" hint="例如 src/**, tests/**；留空表示整个工作区">
                <Input
                  value={scope}
                  onChange={(event) => setScope(event.target.value)}
                  placeholder="src/**, tests/**"
                  spellCheck={false}
                />
              </Field>
              <Field label="超时预算（秒）" htmlFor="newtask-timeout">
                <Input
                  id="newtask-timeout"
                  type="number"
                  inputMode="numeric"
                  min={TIMEOUT_MIN_SEC}
                  max={TIMEOUT_MAX_SEC}
                  step={1}
                  value={timeoutSec}
                  onChange={(event) => {
                    setTimeoutSec(event.target.value);
                    if (timeoutError) setTimeoutError(null);
                  }}
                  placeholder="留空使用类型默认"
                  aria-invalid={timeoutError ? true : undefined}
                  aria-describedby="newtask-timeout-hint"
                />
                <div
                  className={clsx("field-hint", timeoutError && "newtask-field-error")}
                  id="newtask-timeout-hint"
                  role={timeoutError ? "alert" : undefined}
                >
                  {timeoutError ??
                    "30–86400；留空 = 类型默认。只对下一次尝试/运行生效，不会延长正在跑的 Agent。"}
                </div>
              </Field>
              <Field label="最大尝试次数" htmlFor="newtask-max-attempts">
                <Input
                  id="newtask-max-attempts"
                  type="number"
                  inputMode="numeric"
                  min={MAX_ATTEMPTS_MIN}
                  max={MAX_ATTEMPTS_MAX}
                  step={1}
                  value={maxAttempts}
                  onChange={(event) => {
                    setMaxAttempts(event.target.value);
                    if (maxAttemptsError) setMaxAttemptsError(null);
                  }}
                  placeholder="留空使用类型默认"
                  aria-invalid={maxAttemptsError ? true : undefined}
                  aria-describedby="newtask-max-attempts-hint"
                />
                <div
                  className={clsx("field-hint", maxAttemptsError && "newtask-field-error")}
                  id="newtask-max-attempts-hint"
                  role={maxAttemptsError ? "alert" : undefined}
                >
                  {maxAttemptsError ?? "1–10 次；留空 = 类型默认。"}
                </div>
              </Field>
              <Field label="说明">
                <div className="hint-box">
                  任务在隔离副本/worktree 中执行；验收通过后按 git.auto_merge 策略合并回基线，
                  否则保留分支等待人工合并。危险命令会被拦截并等待你确认。
                </div>
              </Field>
            </div>
          </div>
        ) : null}

        {warnings.length ? (
          <div className="warn-box">
            <Wand2 size={14} />
            <div>
              {warnings.map((warning) => (
                <div key={warning}>{warning}</div>
              ))}
            </div>
          </div>
        ) : null}

        <div className="newtask-tip">
          <Sparkles size={13} /> Ctrl + Enter 直接创建并运行
        </div>
      </div>
    </Dialog>
  );
}
