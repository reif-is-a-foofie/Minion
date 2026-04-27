import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { sveltekit } from "@sveltejs/kit/vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;
const isE2E = process.env.VITE_E2E === "1";

const tauriE2eAliases = isE2E
  ? {
      "@tauri-apps/api/core": path.resolve(__dirname, "e2e/stubs/tauri-core.ts"),
      "@tauri-apps/api/event": path.resolve(__dirname, "e2e/stubs/tauri-event.ts"),
      "@tauri-apps/plugin-dialog": path.resolve(__dirname, "e2e/stubs/tauri-plugin-dialog.ts"),
      "@tauri-apps/plugin-updater": path.resolve(__dirname, "e2e/stubs/tauri-plugin-updater.ts"),
      "@tauri-apps/plugin-process": path.resolve(__dirname, "e2e/stubs/tauri-plugin-process.ts"),
    }
  : {};

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [sveltekit()],

  resolve: {
    alias: tauriE2eAliases,
  },

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: isE2E
    ? {
        port: 5173,
        strictPort: true,
        host: "127.0.0.1",
        watch: {
          ignored: ["**/src-tauri/**"],
        },
      }
    : {
        port: 1420,
        strictPort: true,
        host: host || false,
        hmr: host
          ? {
              protocol: "ws",
              host,
              port: 1421,
            }
          : undefined,
        watch: {
          // 3. tell Vite to ignore watching `src-tauri`
          ignored: ["**/src-tauri/**"],
        },
      },
}));
