import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Icon } from "./Icon";

type Kind = "success" | "error" | "info";
interface ToastItem {
  id: number;
  kind: Kind;
  text: string;
}
interface ToastValue {
  notify: (kind: Kind, text: string) => void;
}
const Ctx = createContext<ToastValue | null>(null);
const LIFETIME_MS = 6000;

export function ToastProvider({ children, lifetimeMs = LIFETIME_MS }: { children: ReactNode; lifetimeMs?: number }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(1);
  const dismiss = useCallback((id: number) => {
    setItems((xs) => xs.filter((x) => x.id !== id));
  }, []);
  const notify = useCallback(
    (kind: Kind, text: string) => {
      const id = next.current++;
      setItems((xs) => [...xs.slice(-3), { id, kind, text }]);
      setTimeout(() => {
        dismiss(id);
      }, lifetimeMs);
    },
    [dismiss, lifetimeMs],
  );
  const value = useMemo(() => ({ notify }), [notify]);
  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts" role="region" aria-label="Notifications">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} role={t.kind === "error" ? "alert" : "status"}>
            <Icon name={t.kind === "success" ? "check" : t.kind === "error" ? "alert" : "info"} />
            <span>{t.text}</span>
            <button
              type="button"
              className="icon-btn"
              aria-label="Dismiss notification"
              onClick={() => {
                dismiss(t.id);
              }}
            >
              <Icon name="close" size={16} />
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastValue {
  const v = useContext(Ctx);
  if (v === null) throw new Error("useToast must be used inside <ToastProvider>");
  return v;
}
