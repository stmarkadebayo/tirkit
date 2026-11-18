import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the UI proxies API calls to `tirkit serve` on :8000.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": process.env.TIRKIT_API ?? "http://127.0.0.1:8000" } },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
});
