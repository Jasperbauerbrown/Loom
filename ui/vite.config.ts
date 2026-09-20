import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Built output is committed at the repo root as ui-dist/ and served by the backend at /.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../ui-dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8500", changeOrigin: true },
      "/ws": { target: "http://127.0.0.1:8500", ws: true },
    },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
