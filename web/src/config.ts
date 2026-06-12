// API connection settings: base URL + admin token.
//
// Resolution order:
//   1. Values the operator types into the UI (persisted to sessionStorage —
//      survives navigation, cleared when the tab closes; never localStorage).
//   2. Build-time defaults from Vite env (VITE_ADMIN_API_BASE_URL,
//      VITE_ADMIN_TOKEN) — optional, for local dev convenience only.
//
// No real URL or token value is ever committed; see .env.example.

export interface ApiSettings {
  baseUrl: string;
  adminToken: string;
}

const BASE_URL_KEY = "tocdoc.admin.baseUrl";
const TOKEN_KEY = "tocdoc.admin.token";

function envDefault(key: string): string {
  const val = import.meta.env[key as keyof ImportMetaEnv];
  return typeof val === "string" ? val : "";
}

export function loadSettings(): ApiSettings {
  const storedBase = sessionStorage.getItem(BASE_URL_KEY);
  const storedToken = sessionStorage.getItem(TOKEN_KEY);
  return {
    baseUrl: storedBase ?? envDefault("VITE_ADMIN_API_BASE_URL"),
    adminToken: storedToken ?? envDefault("VITE_ADMIN_TOKEN"),
  };
}

export function saveSettings(settings: ApiSettings): void {
  sessionStorage.setItem(BASE_URL_KEY, settings.baseUrl);
  sessionStorage.setItem(TOKEN_KEY, settings.adminToken);
}

export function clearSettings(): void {
  sessionStorage.removeItem(BASE_URL_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
}

export function isConfigured(settings: ApiSettings): boolean {
  return settings.baseUrl.trim().length > 0 && settings.adminToken.trim().length > 0;
}
