/**
 * Transient notices.
 *
 * Used for things that happened and are already true — "approved by n.ganguli", "copied". A
 * toast must never be the only place a durable fact lives; a review decision, in particular,
 * belongs in the item's own row before it belongs here.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

export type ToastTone = 'accent' | 'accept' | 'warn' | 'danger';

interface Toast {
  id: number;
  text: string;
  tone: ToastTone;
}

interface ToastApi {
  /** Show a notice. Returns immediately; it clears itself after ~4s. */
  push: (text: string, tone?: ToastTone) => void;
}

const ToastContext = createContext<ToastApi>({ push: () => undefined });

/** `const { push } = useToast(); push('approved', 'accept')` */
export function useToast(): ToastApi {
  return useContext(ToastContext);
}

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((text: string, tone: ToastTone = 'accent') => {
    const id = nextId++;
    setToasts((current) => [...current, { id, text, tone }]);
    window.setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 4000);
  }, []);

  const api = useMemo(() => ({ push }), [push]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-host" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast tone-${t.tone}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
