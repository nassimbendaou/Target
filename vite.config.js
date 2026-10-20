import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

const frontend = fileURLToPath(new URL("./frontend/", import.meta.url));

export default defineConfig({
  root: frontend,
  server: {
    port: 5173,
    fs: { allow: [frontend] },
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  preview: {
    port: 4173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
