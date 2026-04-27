/**
 * Browser E2E stubs for @tauri-apps/api/core — only loaded when VITE_E2E=1 (see vite.config.js).
 */
const base = import.meta.env.VITE_E2E_API_BASE as string | undefined;

function apiPort(): number {
  if (!base) return 8765;
  const u = new URL(base);
  return Number(u.port || 80);
}

export async function invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown> {
  if (!base) {
    throw new Error("VITE_E2E_API_BASE is required when using Tauri stubs (set by e2e-desktop-webserver.sh)");
  }
  switch (cmd) {
    case "app_config":
      return {
        data_dir: "/tmp/minion-e2e-stub-data",
        inbox: "/tmp/minion-e2e-stub-inbox",
        api_port: apiPort(),
        api_base: base.replace(/\/$/, ""),
        api_token: "",
        sidecar_bootstrapped: true,
        sidecar_running: true,
      };
    case "restart_sidecar":
      return { pid: 0, api_port: apiPort() };
    case "vision_status":
      return { state: "unavailable", model: "", installed: false, server_up: false };
    case "ensure_vision_model":
      return { state: "unavailable", model: String(args?.model ?? "") };
    case "copy_into_inbox":
      return { drops: [], inbox: "/tmp/minion-e2e-stub-inbox" };
    case "reveal_in_finder":
      return;
    default:
      throw new Error(`E2E invoke not stubbed: ${cmd}`);
  }
}
