/** Parse trailing `:port` from a Minion sidecar base URL. */
export function apiPortFromBase(apiBase: string, fallback = 8765): number {
  const portMatch = apiBase.match(/:(\d+)/);
  return portMatch ? Number(portMatch[1]) : fallback;
}
