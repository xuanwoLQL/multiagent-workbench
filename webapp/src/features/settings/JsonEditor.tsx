import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { AlertTriangle, Braces, Check, RotateCcw, Save, Wand2 } from "lucide-react";
import { Badge, Button } from "../../ui/primitives";
import { useToast } from "../../ui/toast";

interface JsonErrorDetail {
  message: string;
  line?: number;
  column?: number;
  snippet?: string;
}

interface Validation {
  value?: unknown;
  error?: JsonErrorDetail;
}

export function JsonEditor({
  fileName,
  content,
  onSave,
  hint,
  disabled = false,
}: {
  fileName: string;
  content: unknown;
  onSave: (value: unknown) => Promise<void>;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  const editorId = useId();
  const errorId = useId();
  const toast = useToast();
  const initialText = useMemo(() => formatJson(content), [content]);
  const [text, setText] = useState(initialText);
  const [savedText, setSavedText] = useState(initialText);
  const [saving, setSaving] = useState(false);
  const [apiError, setApiError] = useState("");

  useEffect(() => {
    setText(initialText);
    setSavedText(initialText);
    setApiError("");
  }, [fileName, initialText]);

  const validation = useMemo(() => validateJson(text), [text]);
  const dirty = text !== savedText;
  const canSave = dirty && !validation.error && !saving && !disabled;

  const save = async () => {
    if (!canSave || validation.error) return;
    setSaving(true);
    setApiError("");
    try {
      await onSave(validation.value);
      setSavedText(text);
    } catch (error) {
      const detail = errorMessage(error);
      setApiError(detail);
      toast.error(`${fileName} 保存失败`, detail);
    } finally {
      setSaving(false);
    }
  };

  const revert = () => {
    setText(savedText);
    setApiError("");
  };

  const prettify = () => {
    if (validation.error) return;
    setText(formatJson(validation.value));
    setApiError("");
  };

  return (
    <div className="json-editor">
      <div className="json-editor-head">
        <div className="json-editor-title">
          <Braces size={15} />
          <span className="mono">{fileName}</span>
          {validation.error ? (
            <Badge tone="danger" dot>
              JSON 错误
            </Badge>
          ) : dirty ? (
            <Badge tone="warning" dot>
              未保存
            </Badge>
          ) : (
            <Badge tone="success">
              <Check size={11} /> 已同步
            </Badge>
          )}
        </div>
        <div className="json-editor-actions">
          <Button size="sm" variant="ghost" onClick={prettify} disabled={Boolean(validation.error) || saving}>
            <Wand2 size={13} /> 格式化
          </Button>
          <Button size="sm" variant="ghost" onClick={revert} disabled={!dirty || saving}>
            <RotateCcw size={13} /> 撤销
          </Button>
          <Button size="sm" variant="primary" onClick={() => void save()} loading={saving} disabled={!canSave}>
            <Save size={13} /> 保存并生效
          </Button>
        </div>
      </div>

      {hint ? <div className="json-editor-hint">{hint}</div> : null}

      <textarea
        id={editorId}
        className="json-textarea mono"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          setApiError("");
        }}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
            event.preventDefault();
            void save();
          }
        }}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        aria-label={`${fileName} JSON 编辑器`}
        aria-invalid={Boolean(validation.error)}
        aria-describedby={validation.error ? errorId : undefined}
        disabled={disabled}
      />

      {validation.error ? (
        <div className="json-error" id={errorId} role="alert">
          <AlertTriangle size={14} />
          <div className="json-error-main">
            <div className="json-error-title">
              {validation.error.line
                ? `第 ${validation.error.line} 行${validation.error.column ? `，第 ${validation.error.column} 列` : ""}附近格式有误`
                : "JSON 语法错误"}
            </div>
            <div className="json-error-message">{validation.error.message}</div>
            {validation.error.snippet ? (
              <pre className="json-error-snippet">
                {validation.error.line ? `${validation.error.line} | ` : ""}
                {validation.error.snippet}
              </pre>
            ) : null}
          </div>
        </div>
      ) : null}

      {apiError ? (
        <div className="json-api-error" role="alert">
          <AlertTriangle size={13} />
          <span>{apiError}</span>
        </div>
      ) : null}

      <div className="json-editor-foot">
        <span className="mono">{text.split(/\r?\n/).length} 行</span>
        <span>{text.length.toLocaleString("zh-CN")} 字符</span>
        <span className="json-editor-spacer" />
        <span>Ctrl / ⌘ + S 保存</span>
      </div>
    </div>
  );
}

function validateJson(text: string): Validation {
  try {
    return { value: JSON.parse(text) };
  } catch (error) {
    return { error: describeJsonError(text, errorMessage(error)) };
  }
}

function describeJsonError(text: string, message: string): JsonErrorDetail {
  const lineMatch = message.match(/line\s+(\d+)/i);
  const columnMatch = message.match(/column\s+(\d+)/i);
  const positionMatch = message.match(/position\s+(\d+)/i);

  let line = lineMatch ? Number(lineMatch[1]) : undefined;
  let column = columnMatch ? Number(columnMatch[1]) : undefined;

  if ((!line || !column) && positionMatch) {
    const position = Math.max(0, Number(positionMatch[1]));
    const before = text.slice(0, position);
    line = before.split(/\r?\n/).length;
    const lastBreak = Math.max(before.lastIndexOf("\n"), before.lastIndexOf("\r"));
    column = position - lastBreak;
  }

  const snippet =
    line && line > 0
      ? text.split(/\r?\n/)[line - 1]?.trim().slice(0, 240) || undefined
      : undefined;

  return { message, line, column, snippet };
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
