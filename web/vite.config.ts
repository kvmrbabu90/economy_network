import { defineConfig } from "vite";

// Port 5180 picked off Vite's default (5173) so this dev server doesn't fight
// with the half-dozen other Vite projects most laptops accumulate. The Phase 5
// API's CORS rule matches ANY localhost / 127.0.0.1 port, so changing this
// number requires no API edit -- just keep the two sides in sync if you
// override via VITE_API_BASE_URL.
//
// strictPort is OFF: if 5180 is also taken, Vite picks the next free port and
// prints it. Open whatever URL it actually announces.
export default defineConfig({
  server: { port: 5180, strictPort: false },
  build: { sourcemap: true, outDir: "dist" },
});
