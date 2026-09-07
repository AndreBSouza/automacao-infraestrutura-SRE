/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the SAI backend. Defaults to '/api' (the dev proxy). */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
