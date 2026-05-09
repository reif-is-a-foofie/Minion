import { defineConfig } from "vitest/config";

/** Unit tests for pure TS helpers (E2E stays Playwright + Vite stack). */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.{test,spec}.ts"],
    passWithNoTests: false,
  },
});
