/** Stubs @tauri-apps/plugin-dialog — file picker not available in headless browser. */
export async function open(_opts?: unknown): Promise<string | string[] | null> {
  return null;
}

export async function ask(_message: string, _opts?: unknown): Promise<boolean | null> {
  return false;
}
