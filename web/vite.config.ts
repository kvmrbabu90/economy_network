import { defineConfig } from "vite";

// Port 5173 is the one the Phase 5 API's CORS regex already allows.
// Don't change it without also updating api/main.py.
export default defineConfig({
  server: { port: 5173, strictPort: true },
  build: { sourcemap: true, outDir: "dist" },
});
