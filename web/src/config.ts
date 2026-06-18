// API connection settings: base URL + admin token.
//
// Resolution order:
//   1. Values the operator types into the UI (persisted to sessionStorage —
//      survives navigation, cleared when the tab closes; never localStorage).
//   2. Build-time default for the BASE URL only, from Vite env
//      (VITE_ADMIN_API_BASE_URL) — optional, local-dev convenience.
//
// The admin TOKEN deliberately has NO build-time env path: Vite inlines VITE_*
// vars into the built bundle, so a build-time token would ship in the client
// JS. The token is runtime/session-only — entered in the UI, kept in
// sessionStorage. No real URL or token is ever committed; see .env.example.

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

// sessionStorage access can throw in restricted contexts (private mode,
// storage disabled, sandboxed iframe). Guard every call so a hostile/locked
// browser degrades to in-memory settings for the session rather than crashing
// startup or save/clear.
function safeGetItem(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

export function loadSettings(): ApiSettings {
  const storedBase = safeGetItem(BASE_URL_KEY);
  const storedToken = safeGetItem(TOKEN_KEY);
  return {
    baseUrl: storedBase ?? envDefault("VITE_ADMIN_API_BASE_URL"),
    // No env fallback for the token — runtime/sessionStorage only (a VITE_ env
    // token would be inlined into the shipped bundle).
    adminToken: storedToken ?? "",
  };
}

export function saveSettings(settings: ApiSettings): void {
  try {
    sessionStorage.setItem(BASE_URL_KEY, settings.baseUrl);
    sessionStorage.setItem(TOKEN_KEY, settings.adminToken);
  } catch {
    // sessionStorage unavailable — settings remain in memory for this session.
    return;
  }
}

export function clearSettings(): void {
  try {
    sessionStorage.removeItem(BASE_URL_KEY);
    sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // sessionStorage unavailable — nothing persisted to clear.
    return;
  }
}

export function isConfigured(settings: ApiSettings): boolean {
  return settings.baseUrl.trim().length > 0 && settings.adminToken.trim().length > 0;
}
