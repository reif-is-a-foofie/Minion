/**
 * Thin wrapper over @tauri-apps/api so browser-based E2E (Playwright + Vite)
 * can run without the Rust shell: set VITE_E2E=true and VITE_E2E_API_PORT.
 */
import { invoke as tauriInvoke } from "@tauri-apps/api/core";
import { listen as tauriListen } from "@tauri-apps/api/event";

import { apiPortFromBase } from "./net_util";

const E2E = import.meta.env.VITE_E2E === "true";

function e2eApiBase(): string {
  const port = (import.meta.env.VITE_E2E_API_PORT as string | undefined)?.trim() || "8765";
  return `http://127.0.0.1:${port}`;
}

export async function invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown> {
  if (E2E) {
    const apiBase = e2eApiBase();
    const api_port = apiPortFromBase(apiBase);
    switch (cmd) {
      case "app_config":
        return {
          data_dir: "/tmp/minion-e2e-data",
          inbox: "/tmp/minion-e2e-inbox",
          api_port,
          api_base: apiBase,
          api_token: "",
          sidecar_bootstrapped: true,
          sidecar_running: true,
        };
      case "restart_sidecar":
        return { pid: 1, api_port };
      case "vision_status":
        return {
          state: "off",
          model: "",
          installed: false,
          server_up: false,
        };
      case "ensure_vision_model":
        return { state: "off", model: String(args?.model ?? "") };
      case "copy_into_inbox":
        return { drops: [], inbox: "/tmp/minion-e2e-inbox" };
      case "reveal_in_finder":
        return undefined;
      case "screen_context_status":
        return {
          platform: "e2e",
          watcher_supported: false,
          stream_path: "/tmp/minion-e2e-data/screen_context/stream.jsonl",
          last_event: null,
        };
      default:
        throw new Error(`[VITE_E2E] unhandled invoke("${cmd}")`);
    }
  }
  return tauriInvoke(cmd, args);
}

export async function listen<T>(
  event: string,
  handler: (event: { payload: T }) => void,
): Promise<() => void> {
  if (E2E) {
    if (event === "sidecar://status") {
      handler({ payload: { state: "ready", message: "" } as T });
      return () => {};
    }
    if (event === "tauri://drag-enter" || event === "tauri://drag-leave") {
      return () => {};
    }
    return () => {};
  }
  return tauriListen(event, handler);
}
