import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/webhook": "http://localhost:8000",
      "/status": "http://localhost:8000",
      "/approve": "http://localhost:8000",
      "/replay": "http://localhost:8000",
      "/integrations": "http://localhost:8000",
      "/flow": "http://localhost:8000",
      "/stream": "http://localhost:8000",
      "/metrics": "http://localhost:8000",
      "/tickets": "http://localhost:8000",
      "/ticket": "http://localhost:8000",
      "/operate": "http://localhost:8000"
    }
  }
});
