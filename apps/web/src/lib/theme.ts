import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { cssVars, palettes, type Palette, type Theme } from "../styles/tokens";

const KEY = "fx.theme";

function stored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "dark" || v === "light" ? v : null;
  } catch {
    return null; // private browsing throws on access, not just on write
  }
}

function preferred(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/**
 * Tokens are injected as custom properties rather than by swapping a stylesheet,
 * so a theme change repaints without a reflow and without the flash of a second
 * stylesheet loading. The selector is attribute-scoped so it beats the
 * first-paint defaults in styles.css regardless of injection order — Vite moves
 * CSS around between dev and build, and relying on that order is a bug that only
 * shows up in production.
 */
function apply(theme: Theme): void {
  const root = document.documentElement;
  root.dataset.theme = theme;
  let tag = document.getElementById("fx-tokens") as HTMLStyleElement | null;
  if (!tag) {
    tag = document.createElement("style");
    tag.id = "fx-tokens";
    document.head.appendChild(tag);
  }
  tag.textContent = `:root[data-theme="${theme}"]{${cssVars(palettes[theme])}color-scheme:${theme};}`;
}

interface ThemeValue {
  theme: Theme;
  palette: Palette;
  toggle: () => void;
}

const ThemeCtx = createContext<ThemeValue | null>(null);

/**
 * Theme is context, not a free-standing hook. Two components each calling a
 * stateful `useTheme()` would hold two independent copies of the truth, and the
 * chart would keep painting dark candles after the header flipped to light.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => stored() ?? preferred());

  useEffect(() => {
    apply(theme);
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      // Nothing to recover: the theme still works, it just will not persist.
    }
  }, [theme]);

  // Follow the OS only while the user has not expressed a preference of their
  // own. Overriding an explicit choice because the OS flipped at sunset is the
  // kind of "helpful" that people file bugs about.
  useEffect(() => {
    if (stored()) return;
    const mq = window.matchMedia?.("(prefers-color-scheme: light)");
    if (!mq) return;
    const onChange = (e: MediaQueryListEvent) => setTheme(e.matches ? "light" : "dark");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const value = useMemo<ThemeValue>(
    () => ({
      theme,
      palette: palettes[theme],
      toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    }),
    [theme],
  );

  return createElement(ThemeCtx.Provider, { value }, children);
}

export function useTheme(): ThemeValue {
  const ctx = useContext(ThemeCtx);
  if (!ctx) throw new Error("useTheme outside <ThemeProvider>");
  return ctx;
}
