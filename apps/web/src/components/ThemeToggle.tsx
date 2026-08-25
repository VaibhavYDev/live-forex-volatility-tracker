import { useTheme } from "../lib/theme";

/**
 * A switch, not a checkbox: the control has two named states rather than an
 * on/off of one thing, and `aria-pressed` on a button is the pattern screen
 * readers announce correctly without needing a live region.
 */
export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const next = theme === "dark" ? "light" : "dark";

  return (
    <button
      type="button"
      className="toggle"
      onClick={toggle}
      aria-pressed={theme === "light"}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
      <span className="toggle__track" aria-hidden>
        <span className="toggle__thumb" />
      </span>
      <span className="toggle__label">{theme === "dark" ? "Dark" : "Light"}</span>
    </button>
  );
}
