import { defineConfig } from "vite";

// Standalone "Signals" dashboard — separate from the "So What?" graph app. Port 5182 (5180 = graph,
// 5174 = graph alt). The API's CORS allows any localhost port; override the API base with
// VITE_API_BASE_URL if needed.
export default defineConfig({
  server: { port: 5182, strictPort: false },
  build: { sourcemap: true, outDir: "dist" },
});
