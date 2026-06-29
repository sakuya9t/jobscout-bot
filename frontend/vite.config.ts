import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// The SPA is served by FastAPI from app/static. Built asset URLs are prefixed with
// /static/ (mounted as StaticFiles), and index.html is returned by the /app/{rest}
// catch-all. In dev, /api and /health are proxied to the uvicorn backend so the
// httpOnly auth cookie is same-origin from the browser's point of view.
export default defineConfig({
  plugins: [vue()],
  base: "/static/",
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "../app/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
});
