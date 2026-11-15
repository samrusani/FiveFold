import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `npm run dev` serves the frontend with hot reload and passes /api to the viewer's API,
// which `python -m fivefold.viewer` serves on port 8300.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8300" } },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
