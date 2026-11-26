import { defineConfig } from "vite";
export default defineConfig({
  build: { manifest: true, target: "es2022" },
  server: { host: "127.0.0.1", port: 4173 },
});
