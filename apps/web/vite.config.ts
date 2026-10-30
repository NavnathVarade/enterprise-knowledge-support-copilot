/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev only (developer machine): proxy /api to a locally running API so the app stays same-origin (the API has no
// CORS, by design). Production is served by nginx with the same path routing (see nginx.conf).
export default defineConfig({
  plugins: [react()],
  build: { sourcemap: false, target: "es2022" },
  server: { proxy: { "/api": { target: "http://127.0.0.1:8000" } } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
