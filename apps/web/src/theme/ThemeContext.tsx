import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type ThemePreference = "system" | "light" | "dark";
const KEY = "ekc.theme";

function read(): ThemePreference {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Apply the stored preference before first paint so a reload does not flash the wrong theme. */
export function initTheme(): void {
  applyTheme(read());
}

/** `system` removes the attribute so the stylesheet's prefers-color-scheme rules decide. */
export function applyTheme(pref: ThemePreference): void {
  if (pref === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", pref);
}

interface ThemeValue {
  preference: ThemePreference;
  setPreference: (p: ThemePreference) => void;
}
const Ctx = createContext<ThemeValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPref] = useState<ThemePreference>(read);
  useEffect(() => {
    applyTheme(preference);
  }, [preference]);
  const setPreference = useCallback((p: ThemePreference) => {
    setPref(p);
    try {
      if (p === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, p);
    } catch {
      // storage unavailable: the choice still applies for this page view
    }
  }, []);
  const value = useMemo(() => ({ preference, setPreference }), [preference, setPreference]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeValue {
  const v = useContext(Ctx);
  if (v === null) throw new Error("useTheme must be used inside <ThemeProvider>");
  return v;
}
