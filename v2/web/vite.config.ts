import { defineConfig } from "vite";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5174,
    proxy: {
      "/v2": "http://127.0.0.1:8765"
    }
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    proxy: {
      "/v2": "http://127.0.0.1:8765"
    }
  }
});
