import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // The board talks to the API on :8000 during development; in production the
    // API serves the built bundle itself, so this proxy exists only for `npm run dev`.
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
