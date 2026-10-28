/**
 * The browser only ever talks to one origin (Caddy); the API lives under /api/v1.
 * Relative URLs keep cookies same-origin and avoid any CORS configuration.
 */
export const API_BASE = "/api/v1";

export function apiUrl(path: string): string {
  if (!path.startsWith("/")) {
    throw new Error(`API path must start with "/": ${path}`);
  }
  return `${API_BASE}${path}`;
}
