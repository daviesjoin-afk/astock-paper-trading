import { defineConfig } from "vite";

const api = process.env.ASTOCK_V2_API_URL || "http://127.0.0.1:8765";
// `vite preview` serves the browser tests; default it to the isolated test
// port so a manual preview cannot silently read a production instance.
const previewApi = process.env.ASTOCK_V2_API_URL || "http://127.0.0.1:18766";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5174,
    proxy: {
      "/v2": api
    }
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    proxy: {
      "/v2": previewApi
    }
  }
});
