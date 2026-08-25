import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
    css: false,
    restoreMocks: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      // main.tsx is three lines of ReactDOM.createRoot; the test harness and the
      // token table are not product code. Measuring them inflates the number
      // without telling anyone anything.
      // types.ts is declarations only — there is nothing to execute, and v8
      // reports it as 0%, which would drag the number down for no signal.
      exclude: [
        "src/main.tsx",
        "src/lib/stream/types.ts",
        "src/test/**",
        "src/**/__tests__/**",
        "src/**/*.bench.ts",
      ],
      // A ratchet, matching the backend's. Raise it when the number goes up;
      // never lower it to make a build pass.
      thresholds: { lines: 88, functions: 90, branches: 85, statements: 88 },
    },
  },
});
