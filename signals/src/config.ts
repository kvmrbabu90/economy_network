// The EconGraph API base (shared FastAPI backend). Override via VITE_API_BASE_URL at build time.
// localhost resolves to ::1 on Windows, matching uvicorn --host :: .
export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8101";
