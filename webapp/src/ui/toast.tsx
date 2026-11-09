import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import clsx from "clsx";

type ToastKind = "info" | "success" | "error";
interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
  detail?: string;
}

const ToastContext = createContext<{
  push: (kind: ToastKind, message: string, detail?: string) => void;
} | null>(null);

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((kind: ToastKind, message: string, detail?: string) => {
    const id = nextId++;
    setItems((prev) => [...prev.slice(-4), { id, kind, message, detail }]);
    window.setTimeout(() => setItems((prev) => prev.filter((item) => item.id !== id)), 5200);
  }, []);
  const value = useMemo(() => ({ push }), [push]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      {createPortal(
        <div className="toast-stack" role="status" aria-live="polite">
          {items.map((item) => (
            <div key={item.id} className={clsx("toast", `toast-${item.kind}`)}>
              <span className="toast-icon">
                {item.kind === "success" ? (
                  <CheckCircle2 size={15} />
                ) : item.kind === "error" ? (
                  <AlertTriangle size={15} />
                ) : (
                  <Info size={15} />
                )}
              </span>
              <div className="toast-text">
                <div className="toast-message">{item.message}</div>
                {item.detail ? <div className="toast-detail">{item.detail}</div> : null}
              </div>
              <button
                type="button"
                className="toast-close"
                aria-label="关闭"
                onClick={() => setItems((prev) => prev.filter((x) => x.id !== item.id))}
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return {
    info: (message: string, detail?: string) => ctx.push("info", message, detail),
    success: (message: string, detail?: string) => ctx.push("success", message, detail),
    error: (message: string, detail?: string) => ctx.push("error", message, detail),
  };
}
