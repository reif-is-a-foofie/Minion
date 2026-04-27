/** Stubs @tauri-apps/api/event — no Rust event bus in browser E2E. */
export async function listen<T>(
  _channel: string,
  _handler: (ev: { payload: T }) => void,
): Promise<() => void> {
  return () => {};
}
