import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the UI and API share an origin through this proxy, so the
// session cookie is first-party (SameSite=Lax) exactly as it is in production,
// where the static host proxies /api to the API service.
const apiTarget = process.env.RELAY_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.RELAY_WEB_PORT ?? 5173),
    strictPort: true,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: false },
      "/webhooks": { target: apiTarget, changeOrigin: false },
    },
  },
  build: { sourcemap: true, target: "es2022" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
  },
});
