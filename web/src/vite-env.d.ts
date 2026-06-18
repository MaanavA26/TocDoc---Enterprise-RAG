/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ADMIN_API_BASE_URL?: string;
  // No VITE_ADMIN_TOKEN: the admin token must never be a build-time var (Vite
  // inlines VITE_* into the bundle). It is runtime/sessionStorage-only.
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
